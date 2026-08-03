#!/usr/bin/env python3
"""
Grindr API – Explore Messenger
Docs: https://opengrind.org/grindr-api/
"""

import requests
import time

BASE_URL = "https://grindr.mobi"

DEFAULT_HEADERS = {
    "User-Agent": "grindr3/26.9.1.163471;163471;Free;Android 14;Pixel 8;Google",
    "Accept-Language": "en_US",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


class GrindrClient:
    def __init__(self, session_token: str = None):
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.profile_id = None

        if session_token:
            self.session.headers["Authorization"] = f"Grindr3 {session_token}"

    def set_location(self, geohash: str):
        if len(geohash) != 12:
            raise ValueError("Geohash must be exactly 12 characters.")
        resp = self.session.put(f"{BASE_URL}/v4/location", json={"geohash": geohash})
        resp.raise_for_status()
        print(f"Location set to geohash: {geohash}")
        return True

    def get_cascade(self, geohash: str, page: int = 1, page_key: str = None) -> dict:
        params = {"geohash": geohash, "page": page}
        if page_key:
            params["pageKey"] = page_key
        resp = self.session.get(f"{BASE_URL}/v4/cascade", params=params)
        resp.raise_for_status()
        return resp.json()

    def send_text_message(self, target_profile_id: int, text: str) -> dict:
        payload = {
            "type": "Text",
            "target": {"type": "Direct", "targetId": int(target_profile_id)},
            "body": {"text": text},
        }
        resp = self.session.post(f"{BASE_URL}/v4/chat/message/send", json=payload)
        resp.raise_for_status()
        return resp.json()


def main():
    # ========================== FILL THIS IN ==========================
    # Paste your session token between the quotes. Do NOT share it with anyone.
    SESSION_TOKEN = "ya29.a0ARGnu0YuYdBgOFaYJTKPXD1_gvrIzYP3IRWJG7qReG0OrqY5-PnJ48il8JupYAbRBDxSPP4KgjxmF804xbeC6B1B50xt86hZV-Z5kIqqlHzmkN6q-R53NQemGG783DMZo19I6MzGsziVjiPbnfsBMi1xdI5Sswjslm58RcM2_QYBU_95oJbtz4u3M6r9YaYkdHmwnfxBGNcA_O7Z72shB-Tjfv6RI8vqyOQ4DDMJs7mA1xjchkU_xCGLx-Rgl1WiQZrUQhr3F5EzSn4rYDLRSJwJL_4rRpQaCgYKAU4SARISFQHGX2MimESrtY8WOaaEqgFWt7raQw0294"

    # Default: New York City (Times Square area). Change if you want another city.
    EXPLORE_GEOHASH = "dr5r9x9jwu5n"

    # Your message
    MESSAGE_TEXT = "hi, can we talk on snap?? @i-jakeh"
    # =================================================================

    if SESSION_TOKEN == "PASTE_YOUR_SESSION_TOKEN_HERE" or not SESSION_TOKEN:
        print("Error: You need to paste your session token into SESSION_TOKEN.")
        print("Tip: Intercept the Authorization header from the app to get it.")
        return

    client = GrindrClient(SESSION_TOKEN)

    # 1. Set location
    client.set_location(EXPLORE_GEOHASH)

    # 2. Fetch profiles
    profile_ids = []
    page = 1
    page_key = None

    print("Fetching cascade profiles...")
    while True:
        try:
            cascade = client.get_cascade(EXPLORE_GEOHASH, page=page, page_key=page_key)
        except requests.HTTPError as e:
            print(f"Cascade fetch failed: {e}")
            break

        entries = cascade.get("entries") or cascade.get("profiles") or cascade.get("results") or []
        if not entries:
            break

        for entry in entries:
            pid = entry.get("profileId") or entry.get("id")
            if pid:
                profile_ids.append(pid)

        page += 1
        page_key = cascade.get("pageKey") or cascade.get("nextPageKey")
        time.sleep(1.0)

    print(f"Found {len(profile_ids)} profile(s).")

    # 3. Send messages
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
