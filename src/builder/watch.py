import os
import threading
import time
import argparse
from pathlib import Path

from flask import Flask, send_from_directory, abort
from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver

DEBOUNCE_DELAY_SECONDS = 1

def find_index_file(directory, requested_path):
    """
    Look for index.html in the requested directory path
    """
    # Construct the full filesystem path
    full_path = Path(directory) / requested_path.lstrip('/')
    
    # Check if there's an index.html in this directory
    index_path = full_path / 'index.html'
    if index_path.is_file():
        # Return the path relative to the static directory
        return str(Path(requested_path) / 'index.html')
    
    return None

def create_app(output_dir):
    app = Flask(__name__, static_folder=output_dir)

    @app.route('/', defaults={'path': ''})
    @app.route('/<path:path>')
    def serve_file(path):
        # First try to serve the exact file requested
        try:
            return send_from_directory(app.static_folder, path)
        except:
            # If the exact file doesn't exist, look for index.html
            index_path = find_index_file(app.static_folder, path)
            if index_path:
                return send_from_directory(app.static_folder, index_path)
            else:
                abort(404)

    return app

class ChangeHandler(FileSystemEventHandler):
    def __init__(self, build_command):
        self.build_command = build_command
        self.last_modified = 0
        self.lock = threading.Lock()

    def on_any_event(self, event):
        print(f"File {event.src_path} has been {event.event_type}")
        if event.is_directory:
            return
        with self.lock:
            now = time.time()
            if now - self.last_modified > DEBOUNCE_DELAY_SECONDS:
                self.last_modified = now
                os.system(self.build_command)

def start_server(app, port):
    app.run(port=port, use_reloader=False)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Watchdog server with Flask")
    parser.add_argument("-i", "--input", default="./content/", help="Input directory path")
    parser.add_argument("-o", "--output", default="./dist/", help="Output directory path")
    parser.add_argument("-p", "--port", type=int, default=8080, help="Port to run the server on")
    parser.add_argument("-b", "--build-command", default="pdm run src/builder/build.py", help="Build command to run on file changes")

    args = parser.parse_args()

    input_dir = Path(args.input).resolve()
    output_dir = Path(args.output).resolve()

    os.system(args.build_command)

    app = create_app(output_dir)
    server_thread = threading.Thread(target=start_server, args=(app, args.port))
    server_thread.daemon = True
    server_thread.start()

    event_handler = ChangeHandler(args.build_command)
    observer = PollingObserver()
    observer.schedule(event_handler, path=input_dir, recursive=True)
    observer.schedule(event_handler, path=Path(__file__).parent, recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        observer.join()