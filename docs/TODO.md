# Roadmap / TODO

All platforms from the reference screenshots are implemented (see the table in the README).

## Platform follow-ups
- [x] Spotify albums/playlists, YouTube playlists, SoundCloud sets: confirm card, sent one by one
- [ ] `spotify.link` short links tested live; YouTube channel pages as collections
- [ ] Likee: verify from a region where likee.video is reachable; write a custom extractor if yt-dlp's stays broken
- [ ] Snapchat: switch back to yt-dlp when its SnapchatSpotlight extractor is fixed (`use_ytdlp = False` in `sites.py`)
- [ ] Threads: logged-out pages occasionally omit posts; a cookies-based GraphQL fallback would make it robust
- [ ] Instagram stories / highlights (need cookies; gallery-dl supports them)
- [ ] Apple Music / Deezer links (same approach as Spotify: page metadata -> YouTube match)

## Features
- [x] Several links in one message (summary card, one download after another)
- [ ] Inline mode (`@bot <link>`)
- [x] Farsi (default) and English interface with `/language` (`app/i18n`)
- [ ] More interface languages (add a catalog next to `app/i18n/fa.py`)
- [ ] YouTube is throttled (~0.4 MB/s) from the production VPS: cookies or another route would help
- [ ] Admin stats / broadcast, per-user rate limits
- [x] Bounded media retention and clean-up of orphaned files in DOWNLOAD_DIR
- [x] Optional channel-subscription gate configured through `.env`
- [ ] Ads slot
