import json
import os

def main():
    with open('mapping_summary.json', 'r') as f:
        data = json.load(f)
        
    unmatched = data['unmatched']
    
    # Save a report
    artifact_path = r'J:\Users\Dudu\.gemini\antigravity-cli\brain\0a22b243-f0fb-4c3c-aa31-ce2f72ea5953\unmatched_report.md'
    with open(artifact_path, 'w', encoding='utf-8') as f:
        f.write("# Unmatched Folders Report\n\n")
        f.write("Here are the folders that could not be automatically mapped to a destination directory, along with a few sample files from each folder to help identify their contents.\n\n")
        
        f.write("| Thread ID | Folders | Title | Sample Files | Reason |\n")
        f.write("|---|---|---|---|---|\n")
        
        for tid, info in sorted(unmatched.items(), key=lambda x: (x[1]['title'] == "", x[1]['title'], x[0])):
            folders = info['folders']
            title = info['title'] or "*None*"
            
            # Find sample files in these folders
            sample_files = []
            for folder in folders:
                if os.path.exists(folder):
                    files = [fn for fn in os.listdir(folder) if os.path.isfile(os.path.join(folder, fn))]
                    sample_files.extend(files)
            
            samples_str = ", ".join(sample_files[:3])
            folders_str = "<br>".join(folders)
            
            f.write(f"| `{tid}` | {folders_str} | {title} | {samples_str} | {info['reason']} |\n")
            
    print("Report generated successfully.")

if __name__ == '__main__':
    main()
