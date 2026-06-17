#!/usr/bin/env python3
import argparse
import getpass
import html
import http.client
import http.cookiejar
import logging
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

LOGGER = logging.getLogger("download_sciebo_webdav")
CHUNK_SIZE = 1024 * 1024
DEFAULT_RETRIES = 2


class DownloadInterrupted(Exception):
    pass


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
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable detailed debug logging.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Only print warnings, errors, and the progress bar.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"Retry interrupted downloads this many times. Default: {DEFAULT_RETRIES}.",
    )
    return parser.parse_args()


def configure_logging(verbose=False, quiet=False):
    if verbose and quiet:
        raise SystemExit("--verbose and --quiet cannot be used together.")

    if verbose:
        level = logging.DEBUG
    elif quiet:
        level = logging.WARNING
    else:
        level = logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def sciebo_password(password_arg):
    if password_arg:
        LOGGER.debug("Using password provided via command line argument.")
        return password_arg

    password = os.environ.get("SCIEBO_PASSWORD")
    if password:
        LOGGER.debug("Using password from SCIEBO_PASSWORD.")
        return password

    if sys.stdin.isatty():
        LOGGER.debug("Prompting for Sciebo share password.")
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
    LOGGER.debug("Created HTTP opener with cookie handling.")
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))


def authenticate_share(opener, base_url, share_token, password):
    base_url = base_url.rstrip("/")
    quoted_token = urllib.parse.quote(share_token)
    share_url = f"{base_url}/s/{quoted_token}"

    LOGGER.info("Opening Sciebo share page.")
    LOGGER.debug("Share URL: %s", share_url)
    request = urllib.request.Request(share_url, headers=user_agent_headers())
    with opener.open(request) as response:
        LOGGER.debug(
            "Share page response: HTTP %s, %s bytes.",
            response.status,
            response.headers.get("Content-Length", "unknown"),
        )
        share_page = response.read().decode("utf-8", "replace")

    request_token_match = re.search(
        r'name="requesttoken"\s+value="([^"]+)"', share_page
    )
    if not request_token_match:
        if "password-input-form" not in share_page:
            LOGGER.info("Share page does not require password authentication.")
            return
        raise SystemExit("Could not find the Sciebo request token on the password page.")

    request_token = html.unescape(request_token_match.group(1))
    LOGGER.debug("Found Sciebo request token.")
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
        LOGGER.debug("Authentication response: HTTP %s.", response.status)
        authenticated_page = response.read().decode("utf-8", "replace")

    if "password-input-form" in authenticated_page:
        raise SystemExit("Sciebo authentication failed. Check the password.")

    LOGGER.info("Sciebo share authentication succeeded.")


def download_url(opener, url, output):
    LOGGER.info("Starting download.")
    LOGGER.debug("Download URL: %s", url)
    partial_output = f"{output}.part"
    request = urllib.request.Request(url, headers=user_agent_headers())
    interrupted = False
    downloaded = 0
    started_at = time.monotonic()
    progress = None

    with opener.open(request) as response, open(partial_output, "wb") as target:
        try:
            length_header = response.headers.get("Content-Length")
            total_size = int(length_header) if length_header else None
            progress = ProgressBar(output, total_size)

            LOGGER.debug("Download response: HTTP %s.", response.status)
            if total_size:
                LOGGER.info("Remote archive size: %s.", format_bytes(total_size))
            else:
                LOGGER.info("Remote archive size is unknown.")

            first_chunk = response.read(CHUNK_SIZE)
            if first_chunk[:2] != b"PK":
                raise SystemExit(
                    "Sciebo did not return a zip archive. Check that the share is a folder share "
                    "and that the password is correct."
                )

            target.write(first_chunk)
            downloaded += len(first_chunk)
            progress.update(downloaded, started_at)

            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                target.write(chunk)
                downloaded += len(chunk)
                progress.update(downloaded, started_at)
        except http.client.IncompleteRead as error:
            if error.partial:
                target.write(error.partial)
                downloaded += len(error.partial)
                if progress:
                    progress.update(downloaded, started_at)
            interrupted = True
            LOGGER.warning(
                "The server closed the chunked transfer early after %s.",
                format_bytes(downloaded),
            )
        finally:
            if progress:
                progress.finish(downloaded, started_at)

    if interrupted and not zipfile.is_zipfile(partial_output):
        raise DownloadInterrupted(
            f"Interrupted download left an incomplete archive at {partial_output}."
        )

    if not zipfile.is_zipfile(partial_output):
        raise SystemExit(
            "Downloaded file is not a valid zip archive. Check the share token, "
            "password, and Sciebo server response."
        )

    if interrupted:
        LOGGER.warning(
            "Chunked transfer ended abruptly, but the downloaded zip passed validation."
        )

    os.replace(partial_output, output)

    elapsed = max(time.monotonic() - started_at, 0.001)
    LOGGER.info(
        "Download finished: %s written to %s in %s at %s/s.",
        format_bytes(downloaded),
        output,
        format_duration(elapsed),
        format_bytes(downloaded / elapsed),
    )


class ProgressBar:
    def __init__(self, output, total_size):
        self.output = output
        self.total_size = total_size
        self.last_render = 0

    def update(self, downloaded, started_at):
        now = time.monotonic()
        if downloaded and now - self.last_render < 0.1:
            return
        self.last_render = now
        self.render(downloaded, started_at)

    def finish(self, downloaded, started_at):
        self.render(downloaded, started_at)
        print()

    def render(self, downloaded, started_at):
        elapsed = max(time.monotonic() - started_at, 0.001)
        speed = downloaded / elapsed

        if self.total_size:
            percent = min(downloaded / self.total_size, 1.0)
            bar_width = progress_bar_width()
            filled = int(bar_width * percent)
            bar = "#" * filled + "-" * (bar_width - filled)
            message = (
                f"\rDownloading {self.output} [{bar}] {percent * 100:5.1f}% "
                f"{format_bytes(downloaded)}/{format_bytes(self.total_size)} "
                f"{format_bytes(speed)}/s"
            )
        else:
            message = (
                f"\rDownloading {self.output}: {format_bytes(downloaded)} "
                f"at {format_bytes(speed)}/s"
            )

        print(message, end="", flush=True)


def progress_bar_width():
    terminal_width = shutil.get_terminal_size((100, 20)).columns
    return max(10, min(40, terminal_width // 3))


def format_bytes(value):
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(value) < 1024.0:
            return f"{value:3.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TiB"


def format_duration(seconds):
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}h {minutes:02d}m {seconds:02d}s"
    return f"{minutes:d}m {seconds:02d}s"


def download(base_url, share_token, password, output, retries=DEFAULT_RETRIES):
    LOGGER.info("Preparing Sciebo download to %s.", output)
    opener = build_opener()
    authenticate_share(opener, base_url, share_token, password)

    authentication_error = None
    not_found_errors = []
    download_error = None

    for url in build_download_urls(base_url, share_token):
        LOGGER.debug("Trying download endpoint: %s", url)
        for attempt in range(retries + 1):
            if attempt:
                LOGGER.info(
                    "Retrying download endpoint, attempt %d of %d.",
                    attempt + 1,
                    retries + 1,
                )
            try:
                download_url(opener, url, output)
                return
            except urllib.error.HTTPError as error:
                LOGGER.debug("Download endpoint failed with HTTP %s.", error.code)
                if error.code in (401, 403):
                    authentication_error = error
                    break
                if error.code == 404:
                    not_found_errors.append(url)
                    break
                raise
            except (DownloadInterrupted, urllib.error.URLError) as error:
                download_error = error
                LOGGER.warning("Download attempt failed: %s", error)
                if attempt >= retries:
                    break
                continue

    if authentication_error:
        raise SystemExit(
            "Sciebo authentication failed. Check the password and share token."
        ) from authentication_error

    if download_error:
        raise SystemExit(
            "Could not complete the Sciebo folder zip download after retries. "
            f"Last error: {download_error}"
        ) from download_error

    tried_urls = "\n  ".join(not_found_errors)
    raise SystemExit(f"Could not download the Sciebo folder zip. Tried:\n  {tried_urls}")


def main():
    args = parse_args()
    configure_logging(args.verbose, args.quiet)
    if args.retries < 0:
        raise SystemExit("--retries must be zero or greater.")
    download(
        args.base_url,
        args.share_token,
        sciebo_password(args.password),
        args.output,
        retries=args.retries,
    )


if __name__ == "__main__":
    main()
