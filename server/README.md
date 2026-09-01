# Python Backend (BNGC)

Clean FastAPI port of the Node backend.

## Run
``npip install -r requirements.txt
uvicorn server.main:app --host 127.0.0.1 --port 3000 --reload
``n
## Structure
- lib/tokens.py - HMAC tokens
- lib/logger.py - colored logs
- lib/gpbinary.py - Photon parser
- outes/capture.py - authoritative packet handler
- outes/validate_token.py - login

