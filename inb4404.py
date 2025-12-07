#!/usr/bin/python3
# inb4404.py
#
# Lightweight thread watcher/downloader for 4chan-style imageboard threads.
# Primary responsibilities:
# - Monitor a thread (or threads listed in a file) and download new files as they
#   appear.
# - Maintain per-thread and global MD5 hash lists to avoid duplicate downloads.
# - Optionally preserve original filenames when available (--origin-name).
# - Provide options for throttling, reloading, and writing human-friendly metadata.
#
# Notes:
# - Text files are opened explicitly with UTF-8 encoding to safely handle
#   filenames containing non-ASCII characters (emoji, symbols, etc.).
# - The script prefers the site's JSON API (if available) and falls back to
#   HTML scraping when the API is not reachable.

import urllib.request, urllib.error, urllib.parse, argparse, logging
import os, re, time
import http.client
import sqlite3
import fileinput
import hashlib
import json
import base64
import html
from multiprocessing import Process

log = logging.getLogger('inb4404')
workpath = os.path.dirname(os.path.realpath(__file__))
args = argparse.Namespace()

# Path to SQLite database used for global MD5 -> path mapping. Using SQLite
# with WAL journal mode makes concurrent access from multiple processes much
# safer than appending to a plain text file.
DB_PATH = os.path.join(workpath, 'hashes.db')

def init_db():
    """Ensure the SQLite database and the required table exist.

    This function is idempotent and safe to call multiple times. It sets
    `journal_mode=WAL` to improve concurrency between processes.

    Returns:
        None
    """
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        cur = conn.cursor()
        cur.execute('PRAGMA journal_mode=WAL;')
        cur.execute('CREATE TABLE IF NOT EXISTS hashes (md5 TEXT PRIMARY KEY, path TEXT, thread TEXT, ts INTEGER)')
        conn.commit()
    except Exception as e:
        log.warning('Could not initialize hashes DB: ' + str(e))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

def get_md5_path(md5):
    """Return stored path for `md5` or None when not present.

    Args:
        md5 (str): The MD5 hash to look up.

    Returns:
        str or None: The file path associated with the MD5 hash, or None if not found.
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        cur = conn.cursor()
        cur.execute('SELECT path FROM hashes WHERE md5=?', (md5,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def has_md5(md5):
    """Check if the given MD5 hash exists in the database.

    Args:
        md5 (str): The MD5 hash to check.

    Returns:
        bool: True if the hash exists, False otherwise.
    """
    return get_md5_path(md5) is not None


def insert_md5(md5, path, thread_name):
    """Insert md5->path mapping. Uses INSERT OR IGNORE to avoid races.

    Args:
        md5 (str): The MD5 hash of the file.
        path (str): The file path.
        thread_name (str): The name/ID of the thread.

    Returns:
        None
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        with conn:
            conn.execute('INSERT OR IGNORE INTO hashes (md5, path, thread, ts) VALUES (?,?,?,?)', (md5, path, thread_name, int(time.time())))
        conn.close()
    except Exception as e:
        log.warning('Could not write to hashes DB: ' + str(e))


def upsert_md5(md5, path, thread_name):
    """Insert or replace the md5->path mapping.

    Used after dedupe to ensure the DB points to the kept file path.

    Args:
        md5 (str): The MD5 hash of the file.
        path (str): The file path.
        thread_name (str): The name/ID of the thread.

    Returns:
        None
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        with conn:
            conn.execute('INSERT OR REPLACE INTO hashes (md5, path, thread, ts) VALUES (?,?,?,?)', (md5, path, thread_name, int(time.time())))
        conn.close()
    except Exception as e:
        log.warning('Could not upsert into hashes DB: ' + str(e))

def main():
    """Entry point for the script.

    Parses command-line arguments and either starts a watcher for a single
    thread URL or treats the provided argument as a filename containing a
    list of thread URLs (one per line).

    The argument parsing below wires several feature flags and timings that
    control behavior such as throttling between downloads, whether to
    preserve original file names, whether to create a separate 'new'
    directory for recent downloads, and whether to reload the queue file.

    Returns:
        None
    """
    global args
    parser = argparse.ArgumentParser(description='inb4404')

    parser.add_argument('thread', nargs='?', help='url of the thread (or filename; one url per line)')
    parser.add_argument('-c', '--with-counter', action='store_true', help='show a counter next the the image that has been downloaded')
    parser.add_argument('-d', '--date', action='store_true', help='show date as well')
    parser.add_argument('-v', '--verbose', action='store_true', help='show more information')
    parser.add_argument('-l', '--less', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('-n', '--use-names', action='store_true', help='use thread names instead of the thread ids (...4chan.org/board/thread/thread-id/thread-name)')
    parser.add_argument('-r', '--reload', action='store_true', help='reload the queue file every 5 minutes')
    parser.add_argument('-t', '--title', action='store_true', help='save original filenames')
    parser.add_argument(      '--subject', action='store_true', help='use thread subject in directory name')
    parser.add_argument(      '--new-dir', action='store_true', help='create the `new` directory')
    parser.add_argument(      '--refresh-time', type=float, default=20, help='Delay in seconds before refreshing the thread')
    parser.add_argument(      '--reload-time', type=float, default=5, help='Delay in minutes before reloading the file. Default: 5')
    parser.add_argument(      '--throttle', type=float, default=0.5, help='Delay in seconds between downloads in the same thread')
    parser.add_argument(      '--backoff', type=float, default=0.5, help='Delay in seconds by which throttle should increase on 429')
    parser.add_argument(      '--origin-name', action='store_true', help='save files using the original filename when available (strip server numeric prefix when possible)')
    parser.add_argument(      '--dedupe-downloads', action='store_true', help='scan downloads directory, keep oldest file per hash and delete duplicate files')
    args = parser.parse_args()

    # Ensure the SQLite DB exists before starting any workers.
    init_db()

    # If requested, run dedupe pass across the entire downloads directory
    # and then exit. This can be run without providing the positional
    # `thread` argument.
    if getattr(args, 'dedupe_downloads', False):
        dedupe_downloads()
        return

    if args.date:
        logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %I:%M:%S %p')
    else:
        logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s', datefmt='%I:%M:%S %p')    

    if args.less:
        logging.info("'--less' is now the default behavior. Use '--verbose' to increase output detail.")

    if args.title:
        try:
            import bs4
            import django
        except ImportError:
            logging.error('Could not import the required modules! Disabling --title option...')
            args.title = False

    # At this point the `thread` positional may be None (if omitted). If it
    # is missing and we didn't take an early-exit like `--dedupe-downloads`,
    # treat that as a usage error.
    if not args.thread:
        parser.error('the following argument is required: thread (unless --dedupe-downloads is used)')

    thread = args.thread.strip()
    if thread[:4].lower() == 'http':
        download_thread(thread, args)
    else:
        download_from_file(thread)

def load(url):
    """Perform an HTTP GET and return the raw bytes of the response.

    A Request object is used with common headers (User-Agent, Referer,
    Accept-Language etc.) to mimic a modern browser and avoid basic
    anti-bot measures. The referer is derived from the URL's board root so
    that some hosts accept the request.

    Args:
        url (str): The URL to fetch.

    Returns:
        bytes: The raw content of the response.
    """
    parsed = urllib.parse.urlparse(url)
    path_parts = parsed.path.strip('/').split('/')
    referer = f'{parsed.scheme}://{parsed.netloc}/{path_parts[0]}'
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15',
        'Sec-Fetch-Site': 'same-origin',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-User': '?1',
        'Accept-Language': 'en-US,en;q=0.5',
        'Referer': referer,
        'Connection': 'keep-alive',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Priority': 'u=0, i',
        'TE': 'trailers',
    })
    return urllib.request.urlopen(req).read()

def clean_filename(s):
    """Sanitize a string to be safe for use as a filename.

    Removes non-word characters (except for dots, dashes, and spaces) and
    replaces spaces with underscores if Django is not available.

    Args:
        s (str): The string to sanitize.

    Returns:
        str: The sanitized filename.
    """
    try:
        from django.utils.text import get_valid_filename  # prefer Django if available
        return get_valid_filename(s)
    except ImportError:
        s = str(s).strip()
        # remove characters that are not word chars, dot, dash or space
        s = re.sub(r'(?u)[^-\w.\s]', '', s)
        # replace spaces with underscores to produce a safe filename
        s = s.replace(' ', '_')
        if not s:
            s = 'file'
        return s


def get_thread_subject(board, thread_id):
    """Retrieve the subject of a thread (or a comment snippet).

    Attempts to fetch the thread via the 4chan JSON API first. If successful,
    returns the 'sub' (subject) field if present, or a snippet of the 'com'
    (comment) field. If the API fails, attempts to scrape the subject from
    the HTML.

    Args:
        board (str): The board identifier (e.g., 'g', 'wg').
        thread_id (str): The numeric thread ID.

    Returns:
        str or None: The sanitized subject/snippet, or None if retrieval failed.
    """
    # 1. Try JSON API
    try:
        api_url = f'https://a.4cdn.org/{board}/thread/{thread_id}.json'
        req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
        data = urllib.request.urlopen(req).read().decode('utf-8')
        thread_json = json.loads(data)
        posts = thread_json.get('posts', [])
        if posts:
            op = posts[0]
            # Prefer 'sub' (Subject)
            if 'sub' in op:
                return clean_filename(html.unescape(op['sub']))
            # Fallback to 'com' (Comment)
            if 'com' in op:
                comment = op['com']
                # Strip HTML tags
                comment = re.sub(r'<[^>]+>', '', comment)
                comment = html.unescape(comment)
                # Truncate to a reasonable length (e.g. 50 chars)
                if len(comment) > 50:
                    comment = comment[:50].strip() + '...'
                return clean_filename(comment)
    except Exception as e:
        log.debug(f"Failed to fetch subject via API for {board}/{thread_id}: {e}")

    # 2. Fallback to HTML scraping
    try:
        thread_url = f'https://boards.4chan.org/{board}/thread/{thread_id}'
        html_content = load(thread_url).decode('utf-8')
        # Regex for subject: <span class="subject">Subject Here</span>
        match = re.search(r'<span class="subject">([^<]+)</span>', html_content)
        if match:
            return clean_filename(html.unescape(match.group(1)))
    except Exception as e:
        log.debug(f"Failed to fetch subject via HTML for {board}/{thread_id}: {e}")

    return None


def get_title_list(html_content):
    """Parse the HTML content and extract the 'title' attribute from file links.

    This is used when the `--title` flag is set to preserve the filename/title
    supplied in the post rather than the server numeric name. Falls back to
    link text when the title attribute is missing.

    Args:
        html_content (str or bytes): The HTML content of the thread.

    Returns:
        list of str: A list of titles/filenames extracted from the HTML.
    """
    ret = list()

    from bs4 import BeautifulSoup, element as bs4_element
    parsed = BeautifulSoup(html_content, 'html.parser')
    divs = parsed.find_all("div", {"class": "fileText"})

    for i in divs:
        # The structure on typical imageboard HTML is that fileText contains
        # an <a> child describing the file; we take the first direct <a> child.
        # Guard against non-Tag nodes (NavigableString/PageElement) to satisfy
        # static analyzers and avoid attribute errors.
        if not isinstance(i, bs4_element.Tag):
            continue

        anchors = i.find_all("a", recursive=False)
        if not anchors:
            continue

        first_child = anchors[0]
        # Prefer the `title` attribute (original filename) when present,
        # otherwise fall back to the link text.
        # Some find_all results may produce non-Tag nodes (NavigableString / PageElement)
        # which do not implement .get(); check the type first to satisfy static analyzers.
        if isinstance(first_child, bs4_element.Tag):
            title = first_child.get("title")
            if title:
                ret.append(title)
            else:
                ret.append(first_child.text)
        else:
            # Fallback: use the node's string content or its string representation.
            text = getattr(first_child, 'string', None)
            if text:
                ret.append(text)
            else:
                ret.append(str(first_child))

    return ret

def get_md5(file_path):
    """Compute and return the MD5 hex digest of a file's contents.

    Args:
        file_path (str): The path to the file.

    Returns:
        str or None: The MD5 hex digest, or None if the file cannot be read.
    """
    hash_md5 = hashlib.md5()
    try:
        with open(file_path, "rb") as f:
            # Read in chunks to avoid high memory usage on large files.
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
    except IOError:
        return None
    return hash_md5.hexdigest()

def call_download_thread(thread_link, args):
    """Helper wrapper used when spawning a multiprocessing.Process.

    The Process target should be a picklable callable; this thin wrapper lets
    the child process run `download_thread` and simply ignores
    KeyboardInterrupt so cleanup can proceed gracefully in the parent.
    Ensure logging is configured in spawned child processes so that
    per-file download messages are emitted to the console when the
    script is run in "file of links" mode (multiprocessing spawn
    on Windows doesn't inherit the parent's basicConfig). Use the
    same date format selection as the main process.

    Args:
        thread_link (str): The URL of the thread to download.
        args (argparse.Namespace): The parsed command-line arguments.

    Returns:
        None
    """
    try:
        if getattr(args, 'date', False):
            logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %I:%M:%S %p')
        else:
            logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s', datefmt='%I:%M:%S %p')
    except Exception:
        # If logging config fails for any reason, continue anyway and
        # rely on direct prints inside download_thread (log calls are
        # still used elsewhere).
        pass

    try:
        download_thread(thread_link, args)
    except KeyboardInterrupt:
        pass

def download_thread(thread_link, args):
    """Primary watcher function to monitor a thread and download files.

    This runs in either the current process (if invoked directly) or in a
    spawned child process when `download_from_file` starts multiple watchers.

    Responsibilities:
    - Determine board and thread identifiers from the provided URL
    - Create/ensure download directories exist for the thread
    - Load per-thread and global MD5 hash lists to avoid duplicates
    - Poll the thread (via JSON API when possible) for new files
    - Download new files, write them to disk, and update the hash DBs

    Args:
        thread_link (str): The URL of the thread.
        args (argparse.Namespace): The parsed command-line arguments.

    Returns:
        None
    """
    board = thread_link.split('/')[3]
    # thread_id is the numeric ID (e.g. 123456)
    thread_id = thread_link.split('/')[5].split('#')[0]

    # Determine the directory name to use
    thread_dir_name = thread_id

    # Check for existing slug or --use-names flag
    has_slug = len(thread_link.split('/')) > 6
    if has_slug:
        slug = thread_link.split('/')[6].split('#')[0]
        # logic for existing slug support
        if args.use_names or os.path.exists(os.path.join(workpath, 'downloads', board, slug)):
            thread_dir_name = slug

    # Logic for --subject: override directory name with "ID (Subject)"
    if args.subject:
        # Check if we already have a directory matching the ID+Subject pattern to avoid re-fetching
        # or if we need to fetch it.
        # Simple approach: fetch it.
        subject = get_thread_subject(board, thread_id)
        if subject:
            thread_dir_name = f"{thread_id} ({subject})"

    log.info(f'Watching {board}/{thread_id} (Dir: {thread_dir_name})')
    throttle = args.throttle

    directory = os.path.join(workpath, 'downloads', board, thread_dir_name)
    if not os.path.exists(directory):
        os.makedirs(directory)

    # Ensure all_titles is always defined so static analyzers don't report
    # "possibly unbound" errors when the JSON API path is taken and the HTML
    # fallback (which sets all_titles) isn't executed.
    all_titles = []

    # --- MD5 Hashing Logic (per-thread now stored in SQLite DB) ---
    # Per-thread hashes are now queried from the `hashes` table (column
    # `thread`) instead of using a per-thread `.hashes.txt` file. Existing
    # files in the thread directory are scanned and inserted into the DB as
    # needed. Legacy `.hashes.txt` files are removed when found.
    md5_hashes = set()

    # Initialize SQLite-backed global hash DB (replaces the previous
    # `hashes.txt` mechanism). init_db() is idempotent and will create the
    # DB file if it doesn't exist.
    init_db()
    if args.verbose:
        try:
            conn_tmp = sqlite3.connect(DB_PATH, timeout=30)
            cur_tmp = conn_tmp.cursor()
            cur_tmp.execute('SELECT COUNT(*) FROM hashes')
            cnt = cur_tmp.fetchone()[0]
            conn_tmp.close()
            log.info('Loaded ' + str(cnt) + ' global hashes from DB ' + DB_PATH)
        except Exception:
            log.info('No global hash DB found. It will be created as files are downloaded.')

    # Load per-thread hashes from DB if present
    try:
        conn_tmp = sqlite3.connect(DB_PATH, timeout=30)
        cur_tmp = conn_tmp.cursor()
        cur_tmp.execute('SELECT md5 FROM hashes WHERE thread=?', (thread_dir_name,))
        rows = cur_tmp.fetchall()
        md5_hashes.update(r[0] for r in rows if r and r[0])
        conn_tmp.close()
        if args.verbose:
            log.info('Loaded ' + str(len(md5_hashes)) + ' per-thread hashes from DB for ' + board + '/' + thread_dir_name)
    except Exception:
        md5_hashes = set()
        if args.verbose:
            log.info('No per-thread hashes in DB for ' + board + '/' + thread_dir_name + '. Scanning directory for existing files...')

    # Scan the thread directory and integrate any existing files into the
    # per-thread set and the global DB. Also remove legacy `.hashes.txt`.
    for filename in os.listdir(directory):
        full_path = os.path.join(directory, filename)
        if os.path.isfile(full_path):
            if filename == '.hashes.txt':
                # remove legacy file
                try:
                    os.remove(full_path)
                    if args.verbose:
                        log.info('Removed legacy .hashes.txt in ' + directory)
                except Exception:
                    pass
                continue
            file_hash = get_md5(full_path)
            if not file_hash:
                continue
            # If this hash already exists globally and points to a different file, remove the duplicate
            gpath = get_md5_path(file_hash)
            if gpath and os.path.abspath(gpath) != os.path.abspath(full_path):
                try:
                    os.remove(full_path)
                    if args.verbose:
                        log.info('Removed duplicate file: ' + full_path + ' (duplicate of ' + gpath + ')')
                    continue
                except OSError as e:
                    log.warning('Could not remove duplicate file ' + full_path + ': ' + str(e))
            md5_hashes.add(file_hash)
            # Ensure global DB has this entry
            insert_md5(file_hash, full_path, thread_dir_name)

    # --- End MD5 Hashing Logic ---

    # Main polling loop: repeatedly fetch thread metadata, check for new
    # files, and download any new items. The loop sleeps according to
    # `args.refresh_time` between iterations and honors throttling/backoff
    # settings for per-file downloads.
    while True:
        try:
            # Prefer the 4chan JSON API to avoid downloading duplicates
            try:
                # Use thread_id for API calls to ensure it works even if directory is renamed
                api_url = f'https://a.4cdn.org/{board}/thread/{thread_id}.json'
                req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
                api_data = urllib.request.urlopen(req).read().decode('utf-8')
                thread_json = json.loads(api_data)
                posts = thread_json.get('posts', [])
                file_entries = []
                for p in posts:
                    if 'tim' in p and 'ext' in p and 'md5' in p:
                        tim = p['tim']
                        ext = p['ext']
                        api_md5_b64 = p['md5']
                        api_md5_hex = None
                        try:
                            api_md5_hex = base64.b64decode(api_md5_b64).hex()
                        except Exception:
                            api_md5_hex = None
                        filename = (p.get('filename') or str(tim)) + ext
                        file_url = f'https://i.4cdn.org/{board}/{tim}{ext}'
                        # store original filename and tim/ext separately so we can choose original names later
                        file_entries.append((file_url, filename, api_md5_hex, api_md5_b64, p.get('filename'), tim, ext))
                items = sorted(file_entries, key=lambda t: t[1])
            except Exception:
                # fallback to HTML scraping when API fails
                regex = r'(//i(?:s|)\d*\.(?:4cdn|4chan)\.org/\w+/(\d+\.(?:jpg|png|gif|webm|pdf|mp4)))'
                html_result = load(thread_link).decode('utf-8')
                items = list(set(re.findall(regex, html_result)))
                items = sorted(items, key=lambda tup: tup[1])
                if args.title:
                    all_titles = get_title_list(html_result)

            total = len(items)
            count = 1

            for enum_index, enum_tuple in enumerate(items):
                # unpack tuple fields in a backward-compatible way
                link = img = api_md5_hex = api_md5_b64 = original_name = tim = ext = None
                if len(enum_tuple) >= 1:
                    link = enum_tuple[0]
                if len(enum_tuple) >= 2:
                    img = enum_tuple[1]
                if len(enum_tuple) >= 3:
                    api_md5_hex = enum_tuple[2]
                if len(enum_tuple) >= 4:
                    api_md5_b64 = enum_tuple[3]
                if len(enum_tuple) >= 5:
                    original_name = enum_tuple[4]
                if len(enum_tuple) >= 6:
                    tim = enum_tuple[5]
                if len(enum_tuple) >= 7:
                    ext = enum_tuple[6]

                # Logic for naming the file when --title is used
                # Priority:
                # 1. Use original filename from API (if available)
                # 2. Use title extracted from HTML (if available)
                # 3. Fallback to default naming (else block)
                if args.title:
                    imgname = None
                    # Use original_name from API if available. Note: API 'filename' usually lacks extension.
                    if original_name:
                        file_ext = ext if ext else os.path.splitext(img or '')[1]
                        imgname = original_name + (file_ext or '')
                    # Fallback to HTML-extracted title
                    elif 'all_titles' in locals() and len(all_titles) > enum_index:
                        imgname = all_titles[enum_index]

                    if imgname:
                        img_path = os.path.join(directory, clean_filename(imgname))
                    else:
                        # Fallback to normal behavior if we couldn't determine a title
                        # (This duplicates logic from the 'else' block below, but effectively
                        # if imgname is None we just fall through to the else logic?
                        # No, python doesn't support fallthrough. We need to handle the 'else' logic carefully.)
                        pass

                # If args.title was False, OR if it was True but we couldn't find a name (imgname is None)
                if not args.title or (args.title and not locals().get('imgname')):
                    # Allow user to choose original server filename (when available)
                    chosen_name = img
                    if args.origin_name:
                        # if API provided original filename (without extension), use it + ext
                        if original_name:
                            file_ext = ext if ext else os.path.splitext(img or '')[1]
                            chosen_name = original_name + (file_ext or '')
                        else:
                            # try to strip server numeric prefix (tim) if the filename looks like a numeric prefix
                            base = os.path.basename(img or '')
                            stripped = re.sub(r'^[0-9]+(?:[._\-]+)?', '', base)
                            if stripped:
                                chosen_name = stripped

                    # Ensure chosen_name is a valid string for os.path.join
                    if not chosen_name:
                        # fallback to basename(img) or construct from tim+ext or a generic name
                        if img:
                            fallback_name = os.path.basename(img)
                        elif tim:
                            fallback_name = str(tim) + (ext or '')
                        else:
                            fallback_name = 'file'
                        safe_name = fallback_name
                    else:
                        safe_name = chosen_name

                    img_path = os.path.join(directory, safe_name)

                if os.path.exists(img_path):
                    count += 1
                    continue

                # If the JSON API provided the original filename and/or an MD5
                # we can use those to avoid unnecessary downloads.
                # The api_md5_hex check is fast and avoids fetching the file
                # bytes when possible.
                # If we have the API md5 (base64->hex), check before downloading
                if api_md5_hex:
                    # Check the SQLite-backed global DB for known MD5s first
                    gpath = get_md5_path(api_md5_hex)
                    if gpath:
                        # Don't spam INFO for duplicates; use DEBUG so only users
                        # requesting debug-level output see the details.
                        if args.verbose:
                            log.debug('Duplicate (global API md5) skipping %s (MD5: %s)', img, api_md5_hex)
                        count += 1
                        continue
                    if api_md5_hex in md5_hashes:
                        if args.verbose:
                            log.debug('Duplicate (thread API md5) skipping %s (MD5: %s)', img, api_md5_hex)
                        count += 1
                        continue

                # Download the file bytes only when API/previous checks didn't
                # indicate a duplicate. `link` may be protocol-relative
                # (starting with '//') so normalize it to a full URL when
                # necessary.
                # Reduce noisy output: only emit a concise INFO when a new
                # file is actually saved below. Use DEBUG for pre-download
                # details so users can enable verbose logging explicitly.
                if getattr(args, 'verbose', False):
                    display_save = (locals().get('chosen_name') or os.path.basename(img_path))
                    try:
                        log.debug(f'Downloading {display_save} from {board}/{thread_dir_name} (url: {link})')
                    except Exception:
                        log.debug('Downloading file from %s/%s', board, thread_dir_name)
                # Skip items where the link is missing to avoid calling startswith on None
                if not link:
                    if getattr(args, 'verbose', False):
                        log.warning('Skipping item with missing link at index %s in %s/%s', enum_index, board, thread_dir_name)
                    count += 1
                    continue
                if link.startswith('//'):
                    data = load('https:' + link)
                else:
                    data = load(link)
                data_hash = hashlib.md5(data).hexdigest()

                # Double-check after download (consult per-thread set and
                # the SQLite-backed global DB).
                if data_hash in md5_hashes or has_md5(data_hash):
                    # Avoid printing duplicate notices at INFO level; keep them
                    # at DEBUG so normal runs only show newly saved files.
                    if args.verbose:
                        log.debug('Duplicate found after download (MD5: %s), skipping %s', data_hash, img or '')
                    count += 1
                    continue

                # Emit a single, concise INFO line only for newly saved files.
                filename_only = os.path.basename(img_path) or (img or '')
                if args.with_counter:
                    log.info('[%s/%s] NEW: %s/%s', str(count).rjust(len(str(total))), total, board + '/' + thread_dir_name, filename_only)
                else:
                    log.info('NEW: %s/%s', board + '/' + thread_dir_name, filename_only)

                with open(img_path, 'wb') as f:
                    f.write(data)

                # Now that file is saved to disk, update per-thread md5s and
                # persist the global mapping into the SQLite DB.
                md5_hashes.add(data_hash)
                insert_md5(data_hash, img_path, thread_dir_name)

                # Also copy the file into the `new/` directory layout so that
                # external tools can easily pick up newly downloaded images.
                # This is optional and controlled by `--new-dir`.
                if args.new_dir:
                    copy_directory = os.path.join(workpath, 'new', board, thread_dir_name)
                    if not os.path.exists(copy_directory):
                        os.makedirs(copy_directory)
                    # chosen_name may be unbound when --title is used (img_path is set directly),
                    # and static type checkers may consider it None; derive a safe name from img_path.
                    name_for_copy = os.path.basename(img_path) or 'file'
                    copy_path = os.path.join(copy_directory, name_for_copy)
                    with open(copy_path, 'wb') as f:
                        f.write(data)

                # Delay in between image downloads
                time.sleep(throttle)
                count += 1

                pass

        except urllib.error.HTTPError as ex1:
            # 429 Too Many Requests
            if ex1.code == 429:
                log.info('%s 429\'d', thread_link)
                throttle += args.backoff
                sleep_time = 10 + throttle
                time.sleep(sleep_time)
                continue

            try:
                time.sleep(10) # wait before trying again
                load(thread_link)
            except urllib.error.HTTPError as ex2:
                log.info('%s %s\'d', thread_link, str(ex2.code))
                break
            continue
        except (urllib.error.URLError, http.client.BadStatusLine, http.client.IncompleteRead):
            log.fatal(thread_link + ' crashed!')
            raise

        time.sleep(args.refresh_time)

        if args.verbose:
            log.info('Checking ' + board + '/' + thread_dir_name)

def download_from_file(filename):
    """Manage multiple watcher processes for each thread listed in a file.

    Each non-comment, non-disabled line that begins with 'http' is treated
    as a thread URL to be watched in its own Process. A small Lock is
    created for coordination (reserved for future use) and a map of
    running processes is kept so we can detect dead processes and restart
    or disable them in the queue file.

    Args:
        filename (str): The path to the file containing thread URLs.

    Returns:
        None
    """
    from multiprocessing import Process, Lock
    running_processes = {}  # {link: process}
    lock = Lock()

    while True:
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                # a link is valid if it starts with http and not with a '-'
                desired_links = {line.strip() for line in f if line.strip().startswith('http')}
            if getattr(args, 'verbose', False):
                log.info(f'Loaded {len(desired_links)} links from {filename}; {len(running_processes)} watchers currently running.')
                # show which links are new vs already running
                try:
                    current = set(running_processes.keys())
                    new_links = desired_links - current
                    removed = current - desired_links
                    if new_links:
                        log.info('New links to start: ' + ', '.join(sorted(new_links)))
                    if removed:
                        log.info('Links present but not in file: ' + ', '.join(sorted(removed)))
                except Exception:
                    pass
        except FileNotFoundError:
            log.error('File not found: ' + filename)
            break

        if not desired_links and not running_processes:
            log.warning(filename + ' is empty or all links are disabled.')

        # Check for dead processes and mark them for removal from the file
        dead_links = []
        for link, process in running_processes.items():
            if not process.is_alive():
                log.info('Thread ' + link + ' appears to be dead (404\'d or crashed).')
                dead_links.append(link)

        if dead_links:
            # For each dead watcher, probe the thread URL. If the thread
            # returns 404 (deleted) we disable the link in the file by
            # prefixing with '-'. Otherwise attempt a few restarts with
            # backoff; only disable in the file if restarts fail.
            max_restarts = 3
            for link in dead_links:
                log.info('Watcher for ' + link + ' appears to have stopped; probing and attempting restart.')

                # Quick probe: try to load the thread page to detect 404s
                is_404 = False
                try:
                    load(link)
                except urllib.error.HTTPError as he:
                    if getattr(he, 'code', None) == 404:
                        is_404 = True
                except Exception:
                    # Non-HTTP errors are ignored for the probe; we'll try restarts
                    pass

                if is_404:
                    # Disable the link in the file by prefixing with '-'
                    try:
                        with open(filename, 'r', encoding='utf-8') as f:
                            lines = f.readlines()
                        new_lines = []
                        disabled = False
                        for line in lines:
                            stripped = line.strip()
                            if stripped == link and not line.startswith('-'):
                                new_lines.append('-' + line)
                                disabled = True
                                log.info('Disabled ' + stripped + ' in ' + filename + ' (404)')
                            else:
                                new_lines.append(line)
                        if disabled:
                            with open(filename, 'w', encoding='utf-8') as f:
                                f.writelines(new_lines)
                    except IOError as e:
                        log.error('Error writing to file ' + filename + ': ' + str(e))
                    # Ensure process entry is removed
                    running_processes.pop(link, None)
                    continue

                # Not a 404. Try restarting the watcher a limited number of times
                restarted = False
                for attempt in range(1, max_restarts + 1):
                    try:
                        old_proc = running_processes.pop(link, None)
                        if old_proc is not None:
                            try:
                                old_proc.join(timeout=1)
                            except Exception:
                                pass

                        new_proc = Process(target=call_download_thread, args=(link, args, ))
                        new_proc.start()
                        # give it a moment to start
                        time.sleep(1)
                        if new_proc.is_alive():
                            running_processes[link] = new_proc
                            restarted = True
                            if getattr(args, 'verbose', False):
                                log.info(f'Restarted watcher for {link} (attempt {attempt})')
                            break
                        else:
                            # process died immediately; try again after backoff
                            try:
                                new_proc.join(timeout=1)
                            except Exception:
                                pass
                    except Exception as e:
                        log.warning(f'Attempt {attempt} to restart watcher for {link} failed: {e}')
                    time.sleep(5 * attempt)

                if not restarted:
                    # After retries, mark disabled in file so user sees it's dead
                    try:
                        with open(filename, 'r', encoding='utf-8') as f:
                            lines = f.readlines()
                        new_lines = []
                        disabled = False
                        for line in lines:
                            stripped = line.strip()
                            if stripped == link and not line.startswith('-'):
                                new_lines.append('-' + line)
                                disabled = True
                                log.info('Disabled ' + stripped + ' in ' + filename + ' after failed restarts')
                            else:
                                new_lines.append(line)
                        if disabled:
                            with open(filename, 'w', encoding='utf-8') as f:
                                f.writelines(new_lines)
                    except IOError as e:
                        log.error('Error writing to file ' + filename + ': ' + str(e))
                    running_processes.pop(link, None)

        # Start new processes for new links
        for link in desired_links:
            if link not in running_processes:
                log.info('Starting new watcher for ' + link)
                process = Process(target=call_download_thread, args=(link, args, ))
                process.start()
                running_processes[link] = process
            else:
                if getattr(args, 'verbose', False):
                    log.info('Already watching ' + link)

        # Stop processes for links that have been removed from the file
        removed_links = [link for link in running_processes if link not in desired_links]
        for link in removed_links:
            log.info('Link ' + link + ' removed from file. Stopping watcher.')
            running_processes[link].terminate()
            running_processes[link].join(timeout=5)  # Give it a moment to die
            del running_processes[link]

        if not args.reload:
            # If not reloading, wait for all spawned processes to complete.
            # This fixes the orphaned process issue (bug #3).
            for process in running_processes.values():
                process.join()
            break
        else:
            # If reloading, wait for the specified time before checking again.
            if args.verbose:
                log.info('Reloading ' + filename + ' in ' + str(args.reload_time) + ' minutes. Watching ' + str(len(running_processes)) + ' threads.')
            time.sleep(60 * args.reload_time)


def dedupe_downloads():
    """Scan `downloads/` directory and remove duplicate files.

    Computes MD5s for all files, keeps the oldest file for each identical
    content hash, and deletes the rest. Updates the SQLite DB and removes
    legacy per-thread `.hashes.txt` files.

    Returns:
        None
    """
    downloads_root = os.path.join(workpath, 'downloads')
    if not os.path.exists(downloads_root):
        log.warning('No downloads directory found at ' + downloads_root)
        return

    verbose = getattr(args, 'verbose', False)
    md5_map = {}  # md5 -> list of file paths

    if verbose:
        log.info('Scanning files in downloads directory: ' + downloads_root)

    # Collect files and their MD5s
    for root, dirs, files in os.walk(downloads_root):
        for fn in files:
            if fn == '.hashes.txt':
                continue
            full = os.path.join(root, fn)
            if not os.path.isfile(full):
                continue
            h = get_md5(full)
            if not h:
                log.warning('Could not read file for hashing: ' + full)
                continue
            md5_map.setdefault(h, []).append(full)
            if verbose:
                log.info(f'Hashed: {full} -> {h}')

    if verbose:
        log.info(f'Found {sum(len(v) for v in md5_map.values())} files, {len(md5_map)} unique hashes')

    # For each hash group, keep the oldest file and delete duplicates.
    # Also ensure the DB references the kept path for every hash.
    deleted_count = 0
    kept_count = 0
    for h, paths in md5_map.items():
        # sort by mtime ascending (oldest first)
        paths_sorted = sorted(paths, key=lambda p: os.path.getmtime(p))
        kept = paths_sorted[0]
        duplicates = paths_sorted[1:]
        kept_count += 1
        # determine thread name relative to downloads root
        rel = os.path.relpath(kept, downloads_root)
        thread_name = os.path.dirname(rel).replace(os.sep, '/')
        # ensure DB points to the kept path
        upsert_md5(h, kept, thread_name)
        if verbose:
            log.info(f'Hash {h}: keeping {kept}, deleting {len(duplicates)} duplicates')
        for d in duplicates:
            try:
                os.remove(d)
                deleted_count += 1
                log.info('Removed duplicate: ' + d + ' (kept ' + kept + ')')
            except Exception as e:
                log.warning('Failed to remove duplicate ' + d + ': ' + str(e))

    log.info('Dedupe complete. Kept {} groups, removed {} files'.format(kept_count, deleted_count))

    # Remove any legacy per-thread .hashes.txt files (DB now stores per-thread hashes)
    for root, dirs, files in os.walk(downloads_root):
        for fn in files:
            if fn == '.hashes.txt':
                fp = os.path.join(root, fn)
                try:
                    os.remove(fp)
                    log.info('Removed legacy file: ' + fp)
                except Exception as e:
                    log.warning('Could not remove legacy .hashes.txt ' + fp + ': ' + str(e))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
