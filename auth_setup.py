"""
auth_setup.py — One-time OAuth2 setup for direct Gmail API access.

Opens a browser for approval, saves token to data/token.json.
If the automatic callback fails, falls back to manual code entry.

Your client secret JSON is downloaded from the Google Cloud Console:
  APIs & Services → Credentials → OAuth 2.0 Client IDs → Download JSON
Save it as  config/client_secret.json  (or pass --client-secret <path>).

Usage:
    python auth_setup.py
    python auth_setup.py --client-secret /path/to/client_secret.json
"""

import os, argparse
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

BASE       = Path(__file__).parent
DATA_DIR   = BASE / "data"
TOKEN_FILE = DATA_DIR / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/gmail.settings.sharing",
]

DEFAULT_CLIENT_SECRET = (
    os.environ.get("GMAIL_CLIENT_SECRET")
    or str(BASE / "config" / "client_secret.json")
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--client-secret",
        default=DEFAULT_CLIENT_SECRET,
        help="Path to your Google OAuth client secret JSON "
             "(default: config/client_secret.json or $GMAIL_CLIENT_SECRET)",
    )
    args = parser.parse_args()

    client_secret = Path(args.client_secret)
    if not client_secret.exists():
        print(f"ERROR: Client secret not found at: {client_secret}")
        print()
        print("Download it from Google Cloud Console:")
        print("  APIs & Services → Credentials → OAuth 2.0 Client IDs → Download JSON")
        print(f"  Save as: {BASE / 'config' / 'client_secret.json'}")
        print()
        print("Or pass a custom path:  python auth_setup.py --client-secret <path>")
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    creds = None

    # Re-use saved token if still valid
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if creds and creds.valid:
        print("Token already valid. You're good to go.")
        print("Run:  python fetch.py")
        return

    if creds and creds.expired and creds.refresh_token:
        print("Refreshing expired token...")
        creds.refresh(Request())
    else:
        print("Starting OAuth flow...")
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)

        try:
            # Try automatic local server callback first
            print("Opening browser — approve access then return here.")
            creds = flow.run_local_server(port=0, open_browser=True)
        except Exception as e:
            # Fallback: print URL and accept manual code paste
            print(f"\nAutomatic callback failed ({type(e).__name__}).")
            print("Falling back to manual mode:\n")
            flow2 = InstalledAppFlow.from_client_secrets_file(
                str(client_secret), SCOPES,
                redirect_uri="urn:ietf:wg:oauth:2.0:oob"
            )
            auth_url, _ = flow2.authorization_url(prompt="consent", access_type="offline")
            print(f"1. Open this URL in your browser:\n\n   {auth_url}\n")
            code = input("2. Paste the authorisation code here: ").strip()
            flow2.fetch_token(code=code)
            creds = flow2.credentials

    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    print(f"\nToken saved to {TOKEN_FILE}")
    print("Run:  python fetch.py")


if __name__ == "__main__":
    main()
