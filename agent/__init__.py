from dotenv import load_dotenv

# Load .env before any submodule reads env vars at import time (llm.MODEL, db.database_url).
load_dotenv()
