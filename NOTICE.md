# Radio skill notices

This skill does not bundle a stream player or audio recordings. It asks the
owner to select an existing `mpv` executable and uses Wingman's public v3 Audio Library file picker for the user's music selection. The skill source
is licensed under the repository's GPL-3.0 license in `LICENSE`. No mpv,
Wingman, station or music implementation source is copied into the package.

The following direct HTTPS station feeds were verified from the broadcasters'
own primary sources:

- The Base's official listening page lists `https://listen.thebase.sc` (MP3,
  192 kbps), `https://listen.thebase.sc/hq` (AAC-LC, 256 kbps), and
  `https://listen.thebase.sc/portable` (AAC-LC, 64 kbps).
- HCN Radio's official player frontend maps HCN-Radio One (`radio1`, stream ID
  `1`) to `https://hcnradio.ddns.me/stream/1/`. Its published frontend bundle
  constructs `https://hcnradio.ddns.me/stream/${sid}/`; the station table in a
  second official bundle maps Radio One to ID `1`. A read-only HTTPS HEAD
  request returned status 200 and `Content-Type: audio/aacp`.

Verification date: 2026-11-05. Primary source pages and public HCN frontend
bundles:

- <https://thebase.sc/about/how-to-listen>
- <https://www.hcnradio.com/player>
- <https://www.hcnradio.com/_next/static/immutable/chunks/0hee9x5z1a0rj.js>
- <https://www.hcnradio.com/_next/static/immutable/chunks/2w42zidwfxpqe.js>

These are external broadcast streams. Their publication permits listening in
the broadcaster's intended player; it does not grant permission to record,
redistribute or bundle their programming. Station availability and codec
support depend on the current service and selected external player.

## TinyTag metadata reader

TinyTag 2.3.2 reads embedded music tags locally. The release includes the two
unmodified Python files and the MIT license in `dependencies/sc_radio_tinytag/`.
The distinct package directory avoids name collisions with other Wingman skills.
The upstream release is [TinyTag 2.3.2](https://github.com/tinytag/tinytag/releases/tag/2.3.2)
at commit `c123836e950a897bd4bbee04d3700bec8762ca27`. The
[PyPI wheel](https://pypi.org/project/tinytag/2.3.2/) SHA-256 is
`ebaf957915266b9c20414a10f8c5170a345aa039a794650d39d66e7d6b96199f`.
