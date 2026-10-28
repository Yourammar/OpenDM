# OpenDM

A free, open source download manager for Windows, built as an alternative to IDM.

![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)
![Python](https://img.shields.io/badge/python-3.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Multi-connection downloads**: up to 32 connections, with IDM-style dynamic segment splitting
- **Pause and resume**, even after a restart (if the server supports it)
- **Video downloads** from YouTube, Instagram, Facebook, X, TikTok and 1000+ sites via [yt-dlp](https://github.com/yt-dlp/yt-dlp), with a quality picker
- **Browser extension** for Chrome and Edge: download buttons on videos and images, right-click menus, and automatic takeover of browser downloads
- **Smart file naming**: names include the post/video ID, and duplicates get numbers, so no file is ever skipped or overwritten
- Download queue, speed limit, clipboard link catching, and sorting into category folders
- Tray icon, completion popup, and optional start with Windows

## Installation

1. Download **OpenDM-Setup.exe** from the [latest release](../../releases/latest) and run it.
   No admin rights or Python needed. Python, FFmpeg and Deno are bundled.
2. If SmartScreen shows a warning, click **More info → Run anyway**. The installer is not code-signed.
3. Add the browser extension:
   - Open `chrome://extensions` (or `edge://extensions`)
   - Turn on **Developer mode**
   - Click **Load unpacked** and select `%LOCALAPPDATA%\Programs\OpenDM\extension`

## Usage

| Action | How |
|---|---|
| Download a video | Hover over the video and click **Download video** |
| Download an image | Hover over the image and click the round button |
| Download a link or page | Right-click it and choose **Download with OpenDM** |
| Add a URL manually | Open OpenDM and click **Add URL** |

Files are saved to `Downloads\OpenDM\<Category>`.

## File naming

Video names follow a pattern you can change under **Settings → Video file names**.

| Pattern | Example |
|---|---|
| `{uploader} - {title} [{id}]` *(default)* | `natgeo - Sunset over the Alps [C8xYz12AbC].mp4` |
| `{title} [{id}]` | `Sunset over the Alps [C8xYz12AbC].mp4` |
| `{date} - {uploader} - {title} [{id}]` | `2026-10-27 - natgeo - Sunset over the Alps [C8xYz12AbC].mp4` |

Available tags: `{title}` `{uploader}` `{id}` `{date}` `{site}`.
If a name is taken, OpenDM saves the new file as `name (2)`, or replaces the old one if you choose that in Settings.

## Run from source

```bash
pip install -r app/requirements.txt
python app/OpenDM.pyw
```

FFmpeg is recommended for 1080p+ video and MP3 audio (`winget install Gyan.FFmpeg`).

## Project structure

```
app/          Desktop app (Python, Tkinter)
extension/    Chrome/Edge extension (Manifest V3)
installer/    NSIS installer scripts
```

## Security

- The app listens only on `127.0.0.1:9614`. Requests from ordinary websites are rejected.
- Browser cookies are sent only to the local app, and only for login-protected downloads. They are deleted once the download finishes.

## Limitations

- DRM-protected streams (Netflix, Spotify, Prime Video, etc.) cannot be downloaded.
- Sites change often. If video downloads start failing, update yt-dlp.
- The extension is not on the Chrome Web Store yet, so it has to be loaded in Developer mode.

## License

[MIT](LICENSE)
