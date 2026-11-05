<p align="center">
  <img src="assets/sc-radio.svg" alt="SC Radio for Wingman AI" width="900">
</p>

# SC Radio

Play community radio and music from your own library through Wingman AI. Tune in to **The Base** or **HCN Radio**, ask for an album, or shuffle your playlist. Audio plays directly through mpv, with Wingman’s standard custom-skill interface.

**v0.2.0** · **Wingman custom skill API v3** · **Verified on Windows with Wingman 3.2.11** · [GPL-3.0](LICENSE)

**[Download the install ZIP](https://raw.githubusercontent.com/Fowlplaychiken/SC-Radio-Wingman/NewRepo/downloads/sc_radio-0.2.0.zip)** · [Quick start](#install) · [What to say](#use) · [Add stations or music](#configure-stations-and-songs)

| Live radio | Your music | Playback controls |
| --- | --- | --- |
| The Base and HCN Radio come preconfigured. Add a station with its direct HTTPS stream. | Play selected MP3 and WAV files by title, artist, album, or genre. | Shuffle, queue, skip, repeat, mute, and adjust music volume by voice. |

## Install

1. Install mpv from a trusted build listed on the [official mpv installation page](https://mpv.io/installation/). If you download an archive, extract it to a folder you’ll keep.
2. [Download SC Radio](https://raw.githubusercontent.com/Fowlplaychiken/SC-Radio-Wingman/NewRepo/downloads/sc_radio-0.2.0.zip) and extract it. On Windows, paste `%APPDATA%\ShipBit\WingmanAI\custom_skills` into File Explorer’s address bar and copy the extracted `sc_radio` folder there. Open it to check that `default_config.yaml` sits directly inside `sc_radio`. On other platforms, use your Wingman installation’s custom skills folder.
3. Restart Wingman, add **SC Radio** to your profile, and set **mpv executable** in the skill’s settings. On Windows, right-click `mpv.exe` in File Explorer, choose **Copy as path**, and paste it into the field without the surrounding quotes. On Linux, a typical path is `/usr/bin/mpv`.
4. Ask **“Tune in to The Base”** or **“Play HCN Radio.”** Both stations are ready to select. Leave **Station list** at its defaults unless you want to change it.

For your own music, add MP3 or WAV files to Wingman’s **Audio Library**, select them under **SC Radio → Personal playlist**, then ask **“Shuffle my music.”** You can leave the playlist empty if you only use radio.

SC Radio uses the operating system's default audio output. Its initial music
volume is 25%; change it by voice or with the volume command. The player is
started without an mpv configuration file, scripts, video, or YouTube-DL
support. Select only an `mpv` executable you trust.

## Use

Examples you can say or enter as requests:

```text
List my radio stations.
Tune in to The Base.
Play HCN Radio.
What songs, artists, or albums are in my library?
Play <song title> by <artist>.
Put on some <artist>.
Play the album <album> by <artist>.
Play some <genre>.
Shuffle my music.
Play <song title> next.      Add <song title> to the end of the queue.
What's in my queue?          What's playing?
Pause the music.             Resume.                    Stop the music.
Next track.                  Previous track.            Start this song over.
Repeat this song.            Repeat the playlist.       Turn repeat off.
Make the music louder.       Set music volume to 15%.   Mute or unmute the music.
```

Wingman interprets the request from ordinary phrasing. If a profile also uses
another radio or music skill, give it a short preference such as “Use SC Radio
for broadcast stations and personal library music. Use the game radio for
explicit ship controls.” Name SC Radio explicitly to disambiguate a request.

The skill exposes five typed Wingman tools:

- `sc_radio_list` lists stations, songs, artists, albums, or genres.
- `sc_radio_play` plays a station, song, artist, album, genre, or playlist. Music collections can start in order or shuffled.
- `sc_radio_queue` shows the queue or adds a selected song while personal music plays.
- `sc_radio_control` handles playback, next and previous, repeat, mute and status.
- `sc_radio_volume` sets music volume or changes it by a signed percentage-point amount.

Song titles, artist and album names, genres and album track order come from
embedded file tags. Untagged files are playable by filename. If two songs
share a title, give the artist or album as well. The skill controls its own
music player and leaves Wingman's speech volume and audio-device settings alone.
Disabling the skill ends its player session. Some transport controls apply
only to local music.

## Configure stations and songs

Edit **Station list** in the skill settings. Add one station per line, using a
unique name and a direct HTTPS audio feed:

```text
The Base = https://listen.thebase.sc
HCN Radio = https://hcnradio.ddns.me/stream/1/
My station = https://radio.example.org/live.mp3
```

Use a broadcaster's published direct stream URL. A station homepage, embedded
player, or video page is not an audio feed and will not work. SC Radio accepts
up to 32 uniquely named HTTPS stations. The preset feeds were checked against
broadcaster sources; see [`NOTICE.md`](NOTICE.md) for provenance and
verification details. External streams can change, become unavailable, or use
a codec your local player does not support. Live streams may resume at the
current broadcast position after pausing or reconnecting.

For personal music, add files to Wingman's Audio Library and select them in
the **Personal playlist** setting. SC Radio uses only selected MP3 and WAV
files that remain inside that library. It plays audio files you supply. The release ZIP contains the skill and its tag reader.

## Troubleshooting

- **“Set an absolute path to your installed mpv player…”** Check that the setting points to an existing `mpv.exe` on Windows or `mpv` executable on macOS/Linux. A command name such as `mpv` alone is not accepted.
- **Audio does not start:** Confirm mpv opens on your system, the selected operating-system audio output works, and the stream or file is reachable. Try another station or a selected local song to narrow down whether the issue is the feed or player.
- **A station fails to open:** Confirm the URL is a direct HTTPS audio feed, not a webpage. Check the broadcaster's current stream address and whether its audio format is supported by your mpv build.
- **A song is missing:** Confirm it is in Wingman's Audio Library, selected in SC Radio's **Personal playlist** setting, and is an MP3 or WAV file. Then ask Wingman to list the available sources.
- **Pause, status, or volume does not respond:** Ask SC Radio to stop, then start the source again. If Wingman was forcibly terminated, check whether its mpv process is still playing before starting another SC Radio session.
- **Wingman does not show the skill:** Confirm the extracted folder is named `sc_radio`, is inside the custom skills directory, and restart Wingman. Check the Wingman custom skill API version and skill-load logs if the issue remains.

## Remove

Stop playback, remove SC Radio from the Wingman profile, delete the extracted
`sc_radio` folder from the custom skills directory, and restart Wingman. This
does not uninstall mpv or remove music from Wingman's Audio Library; those
remain under your control.

## Source and contributions

This public skill is maintained in the Star Citizen Computer project as an
optional Wingman integration. Its implementation uses Wingman's public custom
skill API v3 and Audio Library picker. The skill does not depend on the Star
Citizen Computer application, game files, game process, or another project
service at runtime. The source and build recipe are included in this
repository; the install ZIP contains the skill and its bundled tag reader.
Install mpv separately and add your own music through Wingman’s Audio Library. See [`NOTICE.md`](NOTICE.md) for station and source
provenance, and [`LICENSE`](LICENSE) for redistribution terms.

Bug reports and contributions should include the SC Radio release version,
operating system, Wingman build, mpv version, and the relevant non-secret
error text. Do not attach personal music, recordings, credentials, or private
library paths. Wingman maintainers can evaluate this as a custom module or
reuse its station and playlist controls in Core. The skill uses the supported
custom-skill interface and patches no Core files.

### Build and test

Use Python 3.11 or newer for development. Wingman supplies Python when running
the skill. To run the tests and build an install ZIP from a standalone checkout:

```shell
python -m pip install pytest PyYAML
python -m pytest -q
python scripts/package.py sc_radio-0.2.0.zip
```

The test suite uses inert Wingman and player fixtures. Live station playback
also requires the broadcaster and your audio output to be available.
