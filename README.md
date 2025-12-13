# inb4404

**inb4404** is a robust, lightweight, and efficient command-line utility for monitoring and downloading media from 4chan-style imageboards. It is a fork of the original [4chan-downloader](https://github.com/Exceen/4chan-downloader) with significant enhancements, most notably a powerful duplicate detection and removal system.

## 🚀 Key Features

*   **Automated Monitoring:** Continuously watches specified threads and downloads new images/videos as they appear.
*   **Intelligent Deduplication:** Uses MD5 hashing to maintain a global database of files. Prevents re-downloading identical files across different threads and can clean up existing archives.
*   **Concurrent Downloading:** Supports watching multiple threads simultaneously via a queue file.
*   **Resilience:** Handles rate limiting (HTTP 429) with exponential backoff and gracefully manages dead threads (404s).
*   **Flexible Naming:** Options to use original filenames, server filenames, or thread titles.
*   **API-First:** Prioritizes the JSON API for performance, falling back to HTML scraping only when necessary.
*   **Cross-Platform:** Pure Python implementation compatible with Windows, macOS, and Linux.

## 📋 Requirements

*   **Python 3.6+**
*   **Standard Library:** No external dependencies are required for core functionality.

**Optional Dependencies:**
For the `--title` feature (saving files with the post title), the following are required:
*   `beautifulsoup4`
*   `django` (specifically for `get_valid_filename`)

## 🛠️ Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/your-username/inb4404.git
    cd inb4404
    ```

2.  **Install optional dependencies (if needed):**
    ```bash
    pip install beautifulsoup4 django
    ```

## 📖 Usage

inb4404 can operate in two primary modes: Single Thread mode and List mode.

### 1. Single Thread Mode
Watch a specific thread by passing its URL directly.

```bash
python inb4404.py https://boards.4chan.org/wg/thread/1234567
```

### 2. List Mode (Batch Processing)
Watch multiple threads defined in a text file.

1.  Create a file (e.g., `queue.txt`) with one URL per line.
2.  Run the script pointing to that file.

```bash
python inb4404.py queue.txt
```

*   **Hot Reloading:** Use the `-r` / `--reload` flag to make the script re-read the file every 5 minutes. This allows you to add or remove threads without restarting the process.
*   **Dead Links:** If a thread 404s, the script will automatically comment it out in the file (prefixing with `-`).

### 3. Deduplication Mode
Scan your existing `downloads/` directory to remove duplicate files, keeping only the oldest copy.

```bash
python inb4404.py --dedupe-downloads
```

## ⚙️ Configuration & Options

| Flag | Long Flag | Description |
| :--- | :--- | :--- |
| `-c` | `--with-counter` | Append a counter to filenames (e.g., `[1/50]`). |
| `-d` | `--date` | Include timestamps in console log output. |
| `-v` | `--verbose` | Enable verbose logging for debugging. |
| `-n` | `--use-names` | Use thread names in directory paths instead of IDs. |
| `-r` | `--reload` | Reload the queue file every 5 minutes. |
| `-t` | `--title` | Save files using the post title (requires optional deps). |
| | `--no-subject` | Exclude the thread subject from the directory name. |
| | `--origin-name` | Save files using the original uploader's filename. |
| | `--new-dir` | Create a separate `new` folder for the latest downloads. |
| | `--refresh-time` | Seconds to wait between thread checks (default: 20). |
| | `--reload-time` | Minutes to wait before reloading the queue file (default: 5). |
| | `--throttle` | Seconds to wait between individual file downloads (default: 0.5). |
| | `--dedupe-downloads` | Run the deduplication tool and exit. |

## 🤝 Contributing

Contributions, issues, and feature requests are welcome!
1.  Fork the project.
2.  Create your feature branch (`git checkout -b feature/AmazingFeature`).
3.  Commit your changes (`git commit -m 'Add some AmazingFeature'`).
4.  Push to the branch (`git push origin feature/AmazingFeature`).
5.  Open a Pull Request.

## 📄 License

Distributed under the MIT License. See `LICENSE` for more information.