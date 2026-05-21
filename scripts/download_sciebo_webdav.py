#!/usr/bin/env python3
import argparse
import getpass
import html
import http.cookiejar
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download a password-protected Sciebo public folder as a zip archive."
    )
    parser.add_argument(
        "--base-url",
        required=True,
        help="Sciebo instance base URL, for example https://rwth-aachen.sciebo.de.",
    )
    parser.add_argument(
        "--share-token",
        required=True,
        help="Public share token from the Sciebo share URL.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Local output zip file.",
    )
    parser.add_argument(
        "--password",
        help="Sciebo share password. If omitted, SCIEBO_PASSWORD is used or an interactive prompt is shown.",
    )
    return parser.parse_args()


def sciebo_password(password_arg):
    if password_arg:
        return password_arg

    password = os.environ.get("SCIEBO_PASSWORD")
    if password:
        return password

    if sys.stdin.isatty():
        return getpass.getpass("Sciebo share password: ")

    raise SystemExit(
        "SCIEBO_PASSWORD is not set and no interactive terminal is available."
    )


def user_agent_headers():
    return {"User-Agent": "vnncomp-setup"}


def build_download_urls(base_url, share_token):
    base_url = base_url.rstrip("/")
    quoted_token = urllib.parse.quote(share_token)
    return [
        f"{base_url}/s/{quoted_token}/download?path=%2F&files=",
        f"{base_url}/s/{quoted_token}/download",
    ]


def build_opener():
    cookie_jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))


def authenticate_share(opener, base_url, share_token, password):
    base_url = base_url.rstrip("/")
    quoted_token = urllib.parse.quote(share_token)
    share_url = f"{base_url}/s/{quoted_token}"

    request = urllib.request.Request(share_url, headers=user_agent_headers())
    with opener.open(request) as response:
        share_page = response.read().decode("utf-8", "replace")

    request_token_match = re.search(
        r'name="requesttoken"\s+value="([^"]+)"', share_page
    )
    if not request_token_match:
        if "password-input-form" not in share_page:
            return
        raise SystemExit("Could not find the Sciebo request token on the password page.")

    request_token = html.unescape(request_token_match.group(1))
    form_data = urllib.parse.urlencode(
        {
            "requesttoken": request_token,
            "password": password,
            "sharingToken": share_token,
            "sharingType": "3",
        }
    ).encode()

    auth_url = f"{base_url}/s/{quoted_token}/authenticate/showshare"
    auth_request = urllib.request.Request(
        auth_url,
        data=form_data,
        headers={
            **user_agent_headers(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with opener.open(auth_request) as response:
        authenticated_page = response.read().decode("utf-8", "replace")

    if "password-input-form" in authenticated_page:
        raise SystemExit("Sciebo authentication failed. Check the password.")


def download_url(opener, url, output):
    request = urllib.request.Request(url, headers=user_agent_headers())
    with opener.open(request) as response, open(output, "wb") as target:
        length_header = response.headers.get("Content-Length")
        total_size = int(length_header) if length_header else None
        downloaded = 0

        first_chunk = response.read(1024 * 1024)
        if first_chunk[:2] != b"PK":
            raise SystemExit(
                "Sciebo did not return a zip archive. Check that the share is a folder share "
                "and that the password is correct."
            )

        target.write(first_chunk)
        downloaded += len(first_chunk)
        print_progress(output, downloaded, total_size)

        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            target.write(chunk)
            downloaded += len(chunk)
            print_progress(output, downloaded, total_size)

        if total_size:
            print()


def print_progress(output, downloaded, total_size):
    if not total_size:
        return

    percent = downloaded * 100 / total_size
    print(f"\rDownloading {output}: {percent:5.1f}%", end="", flush=True)


def download(base_url, share_token, password, output):
    opener = build_opener()
    authenticate_share(opener, base_url, share_token, password)

    authentication_error = None
    not_found_errors = []

    for url in build_download_urls(base_url, share_token):
        try:
            download_url(opener, url, output)
            return
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                authentication_error = error
                continue
            if error.code == 404:
                not_found_errors.append(url)
                continue
            raise

    if authentication_error:
        raise SystemExit(
            "Sciebo authentication failed. Check the password and share token."
        ) from authentication_error

    tried_urls = "\n  ".join(not_found_errors)
    raise SystemExit(f"Could not download the Sciebo folder zip. Tried:\n  {tried_urls}")


def main():
    args = parse_args()
    download(
        args.base_url,
        args.share_token,
        sciebo_password(args.password),
        args.output,
    )


if __name__ == "__main__":
    main()
