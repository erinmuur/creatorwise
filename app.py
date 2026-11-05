import os

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, jsonify
from models import db

app = Flask(__name__)

# Resolve the SQLite path. In production Contextone mounts ./data at /app/data,
# so DATABASE_URL points inside it; locally we fall back to ./data/creatorwise.db.
_db_url = os.environ.get("DATABASE_URL", "")
if not _db_url:
    _data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(_data_dir, exist_ok=True)
    _db_url = "sqlite:///" + os.path.join(_data_dir, "creatorwise.db")
elif _db_url.startswith("sqlite:////"):
    os.makedirs(os.path.dirname(_db_url.replace("sqlite:////", "/")), exist_ok=True)

app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
with app.app_context():
    db.create_all()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    """Liveness probe — Contextone uses this to tell if the container is up."""
    return jsonify(status="ok")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
