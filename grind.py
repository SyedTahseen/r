#!/usr/bin/env python3
"""
Grindr API – Explore Messenger (Email/Password via curl_cffi)
Uses TLS/HTTP-2 fingerprint impersonation to bypass Cloudflare blocks.
"""

from curl_cffi import requests
import time
import sys

BASE_URL = "https://grindr.mobi"

DEFAULT_HEADERS = {
    "User-Agent": "grindr3/26.9.1.163471;163471;Free;Android 14;Pixel 8;Google",
    "Accept-Language": "en_US",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


class GrindrClient:
    def __init__(self):
        # curl_cffi Session with Chrome impersonation
        self.session = requests.Session(impersonate="chrome")
        self.session.headers.update(DEFAULT_HEADERS)
        self.profile_id = None
        self.session_id = None

    def login(self, email: str, password: str) -> dict:
        payload = {
            "email": email,
            "password": password,
            "authToken": None,
            "token": None,
            "geohash": None,
        }
        resp = self.session.post(f"{BASE_URL}/v8/sessions", json=payload)
        resp.raise_for_status()
        data = resp.json()

        self.profile_id = data.get("profileId")
        self.session_id = data.get("sessionId")

        if not self.session_id:
            raise ValueError("Login response missing sessionId")

        self.session.headers["Authorization"] = f"Grindr3 {self.session_id}"
        print(f"Logged in. Profile ID: {self.profile_id}")
        return data

    def set_location(self, geohash: str):
        if len(geohash) != 12:
            raise ValueError("Geohash must be exactly 12 characters.")
        resp = self.session.put(f"{BASE_URL}/v4/location", json={"geohash": geohash})
        resp.raise_for_status()
        print(f"Location set to: {geohash}")
        return True

    def get_cascade(self, geohash: str, page: int = 1) -> dict:
        params = {
            "nearbyGeoHash": geohash,
            "pageNumber": page,
        }
        resp = self.session.get(f"{BASE_URL}/v4/cascade", params=params)
        resp.raise_for_status()
        return resp.json()

    def send_text_message(self, target_profile_id: int, text: str) -> dict:
        payload = {
            "type": "Text",
            "target": {
                "type": "Direct",
                "targetId": int(target_profile_id),
            },
            "body": {"text": text},
        }
        resp = self.session.post(f"{BASE_URL}/v4/chat/message/send", json=payload)
        resp.raise_for_status()
        return resp.json()


def main():
    # ========================== CONFIGURATION ==========================
    EMAIL = "itxtahseen11@gmail.com"
    PASSWORD = "qureshihashmI1$"

    # Default: New York City (Times Square area)
    EXPLORE_GEOHASH = "dr5r9x9jwu5n"

    MESSAGE_TEXT = "Hey! 👋"
    # ===================================================================

    if EMAIL == "your_email@example.com" or not PASSWORD:
        print("Error: Fill in EMAIL and PASSWORD.")
        sys.exit(1)

    client = GrindrClient()

    # 1. Log in
    try:
        client.login(EMAIL, PASSWORD)
    except requests.HTTPError as e:
        print(f"Login failed: {e}")
        if e.response.status_code == 401:
            print("  -> Wrong email or password.")
        elif e.response.status_code == 403:
            print("  -> Still blocked. Try 'chrome_android' instead of 'chrome' on line 22.")
        sys.exit(1)

    # 2. Set location
    client.set_location(EXPLORE_GEOHASH)

    # 3. Fetch cascade
    profile_ids = []
    page = 1

    print("Fetching cascade profiles...")
    while True:
        try:
            cascade = client.get_cascade(EXPLORE_GEOHASH, page=page)
        except requests.HTTPError as e:
            print(f"Cascade failed on page {page}: {e}")
            break

        entries = cascade.get("profiles") or cascade.get("entries") or []
        if not entries:
            break

        for entry in entries:
            pid = entry.get("profileId") or entry.get("id")
            if pid:
                profile_ids.append(int(pid))

        if len(entries) < 50:
            break

        page += 1
        time.sleep(1.0)

    print(f"Found {len(profile_ids)} profile(s).")

    # 4. Send messages
    for pid in profile_ids:
        try:
            client.send_text_message(pid, MESSAGE_TEXT)
            print(f"  ✓ Sent to {pid}")
        except requests.HTTPError as e:
            print(f"  ✗ Failed to send to {pid}: {e}")
            if e.response.status_code == 429:
                print("  ! Rate limited – sleeping 60s")
                time.sleep(60)
        time.sleep(2.5)

    print("Done.")


if __name__ == "__main__":
    main()
