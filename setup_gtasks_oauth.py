#!/usr/bin/env python3
"""
1-Time Google Tasks OAuth Authorization Setup Script
Launches local browser authorization flow to generate token.json for personal Google Tasks sync.
"""

import os
import sys
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = ['https://www.googleapis.com/auth/tasks']
CLIENT_SECRET_FILE = os.path.expanduser("~/OCProjects/voice-notes-pipeline/oauth_client.json")
TOKEN_FILE = os.path.expanduser("~/OCProjects/voice-notes-pipeline/token.json")

def main():
    creds = None
    if os.path.exists(TOKEN_FILE):
        print(f"Existing token found at {TOKEN_FILE}.")
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except Exception as e:
            print(f"Failed to read existing token: {e}")
            creds = None
        
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Refreshing expired credentials...")
            try:
                creds.refresh(Request())
            except Exception as e:
                print(f"Refresh failed: {e}. Re-authenticating.")
                creds = None
                
        if not creds or not creds.valid:
            if not os.path.exists(CLIENT_SECRET_FILE):
                print(f"❌ Error: OAuth client file not found at: {CLIENT_SECRET_FILE}")
                print("\nPlease perform the following steps:")
                print("1. Go to Google Cloud Console (project: voice-notes-503206):")
                print("   https://console.cloud.google.com/apis/credentials")
                print("2. Click 'Create Credentials' -> 'OAuth client ID'")
                print("   - Application type: Desktop App")
                print("   - Name: Voice Notes Tasks")
                print("3. Download the JSON file and save it to:")
                print(f"   {CLIENT_SECRET_FILE}")
                sys.exit(1)
                
            print("🚀 Launching 1-time Google Tasks OAuth authorization flow in browser...")
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
            
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
        os.chmod(TOKEN_FILE, 0o600)
        print(f"\n✅ SUCCESS! Google Tasks OAuth token saved to {TOKEN_FILE}")
    else:
        print("✅ Existing Google Tasks credentials are valid. No action needed.")

if __name__ == "__main__":
    main()
