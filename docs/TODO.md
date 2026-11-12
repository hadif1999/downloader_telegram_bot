# Roadmap / TODO

## Platforms
Each platform implements `app/platforms/base.py:Platform` and is registered in
`app/platforms/registry.py`. Stubs live in `app/platforms/stubs.py` and answer "coming soon".

Most targets are supported by yt-dlp, so the first step is to pull a generic
`YtDlpPlatform` base class out of `youtube.py` (extract → MediaInfo, download with progress)
and subclass it per site.

| Platform  | Library / approach                                                | Notes |
|-----------|-------------------------------------------------------------------|-------|
| Instagram | yt-dlp (reels/posts) + `instaloader` for photos/carousels/stories | needs logged-in cookies, rate limits |
| TikTok    | yt-dlp                                                            | watermark-free formats; photo slideshows need custom code |
| Pinterest | yt-dlp (video pins); plain HTTP for image pins                    | `pin.it` short links must be resolved |
| Snapchat  | yt-dlp (Spotlight)                                                |       |
| Likee     | custom scraper (no stable yt-dlp extractor)                       |       |
| VK        | yt-dlp (`vk.com`, `vkvideo.ru`)                                   | some videos need cookies |
| Facebook  | yt-dlp                                                            | cookies for non-public content |
| Threads   | custom scraper (embedded JSON)                                    | images + videos |
| Music     | `spotdl` for Spotify (matches on YouTube Music), yt-dlp for SoundCloud | send as audio with tags/cover |

## Features
- [ ] Image/album results (send_media_group) for image platforms
- [ ] Inline mode (`@bot <link>`)
- [ ] i18n of bot texts (texts are centralised in `app/services/captions.py`)
- [ ] Admin stats / broadcast, per-user rate limits
- [ ] Periodic clean-up of orphaned files in DOWNLOAD_DIR
- [ ] Optional channel-subscription gate / ads slot
