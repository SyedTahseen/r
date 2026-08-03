#!/usr/bin/env python3
"""
Grindr API – Explore Messenger (Google OAuth variant)
Based on OpenGrind OpenAPI spec (2026-07-20)
"""

import requests
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
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.profile_id = None
        self.session_id = None   # JWT for Authorization header

    def login_with_google_token(self, google_access_token: str) -> dict:
        """
        POST /v8/sessions/thirdparty
        Body: ThirdPartyRequest { thirdPartyVendor: 2, thirdPartyToken, geohash }
        Vendor 2 = Google per the spec.
        """
        payload = {
            "thirdPartyVendor": 2,
            "thirdPartyToken": google_access_token,
            "geohash": None,
        }
        resp = self.session.post(f"{BASE_URL}/v8/sessions/thirdparty", json=payload)
        resp.raise_for_status()
        data = resp.json()

        # If registered is false, this Google identity has no Grindr account yet.
        if not data.get("registered", True):
            raise RuntimeError(
                "This Google account is not linked to a Grindr account yet. "
                "You need to create one via the app first."
            )

        auth = data.get("authenticationResponse", {})
        self.profile_id = auth.get("profileId")
        self.session_id = auth.get("sessionId")   # This is the JWT!

        if not self.session_id:
            raise ValueError("Login response missing sessionId")

        self.session.headers["Authorization"] = f"Grindr3 {self.session_id}"
        print(f"Logged in via Google. Profile ID: {self.profile_id}")
        return data

    def use_existing_jwt(self, jwt_token: str):
        """Use a previously obtained sessionId JWT directly."""
        self.session_id = jwt_token
        self.session.headers["Authorization"] = f"Grindr3 {jwt_token}"

    def set_location(self, geohash: str):
        """PUT /v4/location"""
        if len(geohash) != 12:
            raise ValueError("Geohash must be exactly 12 characters.")
        resp = self.session.put(
            f"{BASE_URL}/v4/location",
            json={"geohash": geohash}
        )
        resp.raise_for_status()
        print(f"Location set to: {geohash}")
        return True

    def get_cascade(self, geohash: str, page: int = 1) -> dict:
        """GET /v4/cascade — nearbyGeoHash is required per spec."""
        params = {
            "nearbyGeoHash": geohash,
            "pageNumber": page,
        }
        resp = self.session.get(f"{BASE_URL}/v4/cascade", params=params)
        resp.raise_for_status()
        return resp.json()

    def send_text_message(self, target_profile_id: int, text: str) -> dict:
        """POST /v4/chat/message/send"""
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
    # Paste your Google OAuth token here (the ya29... string)
    GOOGLE_ACCESS_TOKEN = "ya29.a0ARGnu0YuYdBgOFaYJTKPXD1_gvrIzYP3IRWJG7qReG0OrqY5-PnJ48il8JupYAbRBDxSPP4KgjxmF804xbeC6B1B50xt86hZV-Z5kIqqlHzmkN6q-R53NQemGG783DMZo19I6MzGsziVjiPbnfsBMi1xdI5Sswjslm58RcM2_QYBU_95oJbtz4u3M6r9YaYkdHmwnfxBGNcA_O7Z72shB-Tjfv6RI8vqyOQ4DDMJs7mA1xjchkU_xCGLx-Rgl1WiQZrUQhr3F5EzSn4rYDLRSJwJL_4rRpQaCgYKAU4SARISFQHGX2MimESrtY8WOaaEqgFWt7raQw0294"

    # 12-char geohash for the city you want to Explore
    # Default: dr5r9x9jwu5n (New York City, Times Square area)
    EXPLORE_GEOHASH = "dr5r9x9jwu5n"

    # Your message
    MESSAGE_TEXT = "Hey! 👋"
    # ===================================================================

    client = GrindrClient()

    # Step 1: Exchange Google token for Grindr session
    try:
        client.login_with_google_token(GOOGLE_ACCESS_TOKEN)
    except requests.HTTPError as e:
        print(f"Google login failed: {e}")
        if e.response.status_code == 401:
            print("  -> The Google token is expired or invalid. Get a fresh one from the OpenGrind app.")
        sys.exit(1)
    except RuntimeError as e:
        print(e)
        sys.exit(1)

    # Step 2: Teleport to Explore location
    client.set_location(EXPLORE_GEOHASH)

    # Step 3: Paginate through cascade
    profile_ids = []
    page = 1

    print("Fetching cascade profiles...")
    while True:
        try:
            cascade = client.get_cascade(EXPLORE_GEOHASH, page=page)
        except requests.HTTPError as e:
            print(f"Cascade failed on page {page}: {e}")
            if e.response.status_code == 401:
                print("  -> Session expired. Get a fresh Google token.")
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

    # Step 4: Send messages
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
