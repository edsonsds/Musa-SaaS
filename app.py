from flask import Flask, render_template
import os

app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/health')
def health():
    return {'status': 'ok', 'app': 'Musa SaaS'}

if __name__ == '__main__':
    app.run(debug=False)
