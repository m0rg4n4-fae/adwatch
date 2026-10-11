# Changelog

## Unreleased (since 0.1.0)

Everything below has been added since the first GitHub release (`c248923`, "adwatch 0.1.0", Oct 8 2026).

### Supervised run fixes (Oct 10)
- **No more waiting out the minimum after a video has visibly finished.** When an ad's progress bar runs to the end, its X is tapped after `bar_done_min_seconds` (8) instead of the game's full minimum (30 s for Power Spin Quest after a reward ad). A TikTok ad sat with its X showing for 19 seconds. If a reward ad still objects, its "lose your reward" box gets RESUME as before.
- **More pictures ship with adwatch** (`adwatch/starter/`): 11 more close buttons (plain X's and the blue "CLOSE"), 5 Google Play badges (including the faint grey pill and the round "▶|" one), Google's "Reward granted ✕" and a "▶▶|" skip button. They were picked by their record in real runs, and none have the ad's own background baked in.
- **Other phones:** a new test replays real ad screenshots resized to 720p (budget phones such as a Moto G or TCL) and 1440p, and checks adwatch finds the same buttons in the same places. Two stale tests were fixed too (they had stopped matching how adwatch reads the phone).
- **A progress bar that vanishes part-way** no longer lets the ad loop start tapping close buttons 4 seconds later: while the last bar seen was below `bar_close_fraction`, it holds off for up to `bar_stall_seconds` (15). A reward ad got a "Close ad? You will lose your reward" this way.
- **The way to the ad button can take several taps.** `open_templates` can list a route, such as a menu button and then an item in that menu. When the ad button isn't showing, adwatch taps the furthest step along that it can see, so a menu that's already open isn't closed again. `open_wait_seconds` gives a loading screen time to finish.
- **Ads the game shows by itself** (when it starts, between screens) while the ad loop is looking for the ad button are now dealt with first, instead of the button search failing.
- **No more BACK on the game's own screen when recovering.** In Power Spin Quest, BACK there brings up "Leave the game? Are you sure?". adwatch now goes straight to restarting the game instead.
- **A "close" picture that sits on the store badge** (the same play-and-bar symbol) now counts as a badge press when it opens the store: it's the way out, so it isn't marked as a decoy and the ad gets no breather.
- Progress bars along the **top** of the screen are now noticed too (Google's AdMob end screens show a yellow one there and only show their X when it fills). adwatch used to think those ads had ended and press BACK. Each edge is tracked on its own, so a still coloured strip at one edge can't hide a moving bar at the other.
- adwatch keeps a track record of "nearly matching" close pictures (in `decoys.json`). A picture whose near-matches have been tapped 6 times without the ad ever closing is no longer tapped on a near-match (a full match still counts). The ">>" arrow had been tapped 20 times for nothing. Change the number with `near_miss_tries`.
- The note that the wake press is being skipped on a kind of ad is shown once per run instead of on every ad.
- When a press on the store badge does nothing, the second try is a plain quick tap instead of a longer press (the MISTPLAY ad's "Google Play" pill ignored held presses but works with a finger tap). If that does nothing either, the screen is saved to `debug/`.
- The wake press is only tried 3 times on an ad where no badge has shown up (it used to be up to 15). On ads without a fading badge it was pressing the ad itself, which opened the store. Change it with `wake_tries_without_badge`.
- adwatch remembers how the wake press does on each kind of ad (by its screen name, in `decoys.json`). Where it has opened the store twice and never brought the badge up (Google's AdMob video ads, where any touch is a click on the ad), it isn't used at all.
- When a tap opens the store, the screen it was made on is saved to `debug/` with the spot circled, so the culprit can be found.

### Ads you leave through the Play Store (Oct 10)
- Pressing the store badge often opens the Play Store as a sheet *over* the ad, and Android still reports the ad as the screen in front. adwatch used to think the press hadn't worked and press again, inside the store sheet. It then took the store's own "Close sheet" for an ad button and called it a decoy. Now:
  - It waits up to 5 seconds for the store.
  - It checks the phone's screen tree for a store sheet and backs out of it with BACK, counting that as the badge's intended trip.
  - It only presses again if the badge is still showing and the ad is still in front.
- Buttons belonging to the Play Store (or Galaxy Store) are never taken for ad buttons.
- A store trip within 20 seconds of a badge press counts as the badge's trip, even if the store was slow to open. It isn't blamed on anything and gets no breather.
- After any other store trip (for example the wake press opening the store), a badge that's now showing is pressed straight away instead of after `stuck_seconds`. A trip caused by the wake press gets no breather.
- Near-miss counts start over after a store trip.

### "Close Ad? You will lose your reward" (Oct 10)
- When the ad's X is tapped too early, Google shows this box. adwatch now taps **Resume**, never Close, and leaves the ad alone for `reward_wait_seconds` (30). It also raises that game's earliest close time (`passive_min_seconds` in watch mode, `ad_min_seconds` in the ad loop) to 30 in its profile, so it doesn't happen again.
- The box is read from the phone's screen tree, or recognised by a picture of its Resume button, which ships in the starter set.
- A title like "Close Ad?" no longer counts as a close button (it used to get tapped, doing nothing). Neither does any label that is a question or a sentence.
- A near-miss that was tapped and didn't close the ad isn't tapped again in that spot.

### Keep the computer awake (Oct 10)
- `run`, `watch` and `keepalive` stop the computer from sleeping while they run: a `systemd-inhibit` lock (sleep and idle) on Linux, `caffeinate` on macOS, a power request on Windows. The screen can still turn off.
- On by default. Switch it off under **Settings → Keep this computer awake while running** (applies to all games), or per run with `--no-keep-awake`. `--keep-awake` turns it on for one run.
- The lock goes away by itself the moment adwatch exits, even after a crash or a kill. Under `keepalive` one lock covers the restarts and the wait for a fix.
- If the system refuses the lock, adwatch says why and runs anyway.

### Game pop-ups (Oct 9)
- **"Misc buttons"**: the game's own pop-ups (a level-up OK, a daily reward...) can be saved per game with what to do about each one: tap it, tap beside it, swipe it up/down/left/right, or press Back. Save them with **Teach it buttons → Game pop-up** or `grab GAME NAME --misc --action ...`, and remove them with **Forget a game pop-up**.
- They're dealt with before each ad, whenever one hides the ad button, when one shows up right after an ad (which also counts as the game being back), while recovering from a failed step, and every `misc_check_seconds` (5) in watch mode. At most `misc_max_per_minute` (8) a minute.
- A game with none saved takes no extra screenshots.

### Ad handling rework (Oct 9)
Worked out from 16 ads that beat adwatch on Oct 9 and logs from a bot that uses adwatch as a library.
- **Landscape ads are matched at the right size.** The screen used to be shrunk by its long side, so in landscape every saved picture was 2.2x too big to fit. Pictures saved from a landscape screen had the opposite problem: they were shrunk to a few pixels, which is why the dark ringed X's (`close_27`, `close_30`, `close_32`) "matched" random dark spots mid-video and opened the store.
- **Close buttons are looked for near the screen's edges, and at a few sizes.** A match away from the edges must score 0.96. When nothing matches at the saved size, the closest pictures are tried 0.8x to 1.25x.
- **Near-misses are tapped.** A picture that scores at least 0.80, in the same spot on 3 looks in a row, on a screen that isn't playing, is tapped. The puzzle ad's "Reward granted X" sat at 0.84 for over a minute. This also works for faded store badges.
- **Decoys are blamed by spot.** A picture that opens the store is skipped in that spot only, not everywhere. Spots that did it in two ads are remembered in `decoys.json` across runs.
- **Skip buttons** (`skip_*.png`): Skip, >> and "Reward granted X" buttons are tapped about 2 seconds in, even while the ad is still playing. They can be saved with `grab --skip`, `k` while paused, or **Teach it buttons → Skip button**. Your `close_34`, `close_40`, `close_42` and `close_43` were copied as `skip_1` to `skip_4`, and `tidy` removes the old copies.
- **One "is it still playing?" decision** replaces the overlapping checks:
  - It is playing while a progress bar grows or a countdown ring in a corner ticks. The countdown ring is newly detected.
  - It has ended when the bar stopped, when a video stopped changing, or when the picture never moved at all (playables, surveys, end cards).
  - Small animations (a pointing hand, a pulsing button) don't count as a video still playing.
  - Close buttons are no longer held back just because something on screen moves.
- **Faster give-up on ads with no way out.** An ad that has ended and shows nothing to tap gets Back after 20 seconds (`no_exit_seconds`) and is given up on after 2 Back presses. That takes about 60 seconds instead of about 105, and the ad loop then restarts the game.
- **The store badge is remembered.** It is watched for from the start. If it has faded by the time it's needed, its old spot is pressed, after the wake press when one is set.
- **The game counts as back when the screen matches the game's screen from before the ad**, even if none of its saved pictures show.
- **Android's "Reward granted" label** counts as a close button, and its X is pressed.
- **The "which screen is in front" check** goes through the open fast shell, about half the time of a new adb call.
- **Ad statistics**: ads watched (and given up) per game, per phone and in total, with today's count and time spent, in `stats.json`. See them with `adwatch stats` or **Ad stats** in the menu. A run ends with the game's all-time count. Ads watched by other scripts that use adwatch as a library are counted too.

### New
- **Unattended runs.** `adwatch keepalive run GAME ...` (or `keepalive watch ...`) restarts adwatch after a crash. If it stops because of repeated problems, it waits up to `--wait-hours` (default 4) for a fix: drop a file called `restart` in `control/` to start it again, or `stop` to end it.
  - Everything goes to `logs/adwatch.log`, including crash tracebacks.
  - The screen is saved to `debug/` whenever something goes wrong.
  - adwatch exits with code 2 when it stops on repeated failures.
- **Detailed debug log.** `logs/debug.log` is always written. It records screen-grab and matching times, every score, tap and key, and why each decision was made, starting with a session header for each run. It rotates at 5 MB. Add `-v` to print it live.
- **Video feed.** `--feed /dev/video9` reads the phone's screen from a scrcpy v4l2loopback virtual webcam in a few milliseconds instead of about half a second. If the feed can't be opened or stops, adwatch goes back to adb screenshots by itself.
- **Pop-ups over ads.** Teach it an ad pop-up (`grab --popup`, `p` while paused, or the menu), such as an "Install" bubble, and adwatch taps beside it whenever it shows up. `dismiss_tap` can fix that spot.
- **`adwatch tidy [--yes]`.** Removes duplicate pictures, template folders no game uses, and old debug screenshots. It asks before deleting.
- **Game restart when stuck.** When the ad loop gives up on an ad, or can't get back to the game, it force-stops the game, reopens it and carries on, up to `max_restarts` times per run.
- **Meta ads.** Facebook Audience Network ads run inside the Facebook app. They are now recognised as ads and closed in both modes.

### Smarter ad handling
- **Store-trip blame.** Every trip to the Play Store is blamed on the tap that caused it: a wake press, a labelled button, a close picture, a pop-up tap or a store badge. That tap isn't used again on that ad.
  - Close pictures that send you to the store, like Match Factory's fake "Skip Video", are remembered as decoys.
- **Back-off.** After each store trip the ad gets a breather: 10 seconds per trip so far, up to 40.
- **Giving up.** adwatch gives up on an ad after `max_store_trips` (5) trips or `ad_hard_max_seconds` (240) seconds, instead of looping forever.
- **Progress bars.** While a progress bar along the bottom of the ad is still moving, nothing risky is tried.
- **Wake presses register more reliably.**
  - Points too close to the top or bottom edge, which Android swallows, are moved inside the safe area.
  - The press is held for 120 ms (`press_ms`).
  - After a press, adwatch keeps checking for the store badge for 4 seconds.
  - Up to 15 wake presses per ad.
- **Store badges.**
  - They have their own match strictness, `stuck_threshold` (0.8, or `threshold` if that is lower).
  - If a press on "Open Store" or a similar badge doesn't register, it is pressed again.
- **Labelled buttons.** Android's own "Close"/"Skip" buttons are read from the UI tree at most every 6 seconds (`ui_check_seconds`), and the same one isn't tapped more than twice.

### Faster
- **Matching is about 10× faster.** Pictures are compared at a fixed working size, searched at half resolution first and then refined. Blank areas are skipped, and the most recently matched pictures are tried first.
- **Duplicates are skipped.** Identical template pictures are only matched once, and the starter set no longer copies in pictures you already have.
- **Raw screenshots.** `--screen raw|png|auto` picks the adb screenshot format. `auto`, the default, times both once and uses the faster one.
- **Fast taps.** Taps go through one long-lived adb shell (`cmd input` on Android 11+) instead of a new adb command each time. `--no-fast-taps` switches back.

### Fixed
- Plain black or flat areas of the screen no longer "match" a saved picture.
- A failed UI-tree read no longer reuses the previous screen's buttons.
- A failed screenshot while writing the log no longer stops the run.

### Also in your local copy (not yet in the README or the tests)
These are in `adwatch/cli.py` on your computer but weren't part of the changes listed above. All of them except the chooser fix are off unless a profile turns them on:
- **"Open with" chooser.** When an ad's store link brings up Android's "Open with" sheet on a phone with more than one store, it now counts as leaving the game.
- **`bar_close_fraction`** (0.75). In the ad loop, close pictures and labelled buttons aren't tapped until a moving progress bar has passed this point.
- **`still_before_close`** (3 seconds). Now only used to decide that a video has ended. It no longer holds back close buttons, and `still_give_up_seconds` is no longer used.
- **`finished_min_seconds`** (off). An ad that is seen to have ended can be closed before `ad_min_seconds`.
- **`answer_templates`** and common pictures named `answer_*.png`**, with `max_answer_taps` (12). Survey-style ads get their answer and Done buttons tapped before any close button.
- **`skip_templates`.** Close pictures listed here are real skip buttons, usable while the ad is still playing.

### New settings
| Key | Default | Meaning |
|---|---|---|
| `max_store_trips` | 5 | give up on an ad after this many store trips |
| `ad_hard_max_seconds` | 240 | give up on an ad after this long |
| `restart_game_when_stuck` | true | ad loop: reopen the game after giving up on an ad |
| `max_restarts` | 5 | ad loop: most game restarts per run |
| `restart_wait_seconds` | 15 | how long the game gets to load after a restart |
| `stuck_threshold` | 0.8 or `threshold`, whichever is lower | match strictness for store badges |
| `press_ms` | 120 | how long a wake press is held |
| `ui_check_seconds` | 6 | how often the UI tree is read for labelled buttons |
| `popup_templates`, `dismiss_tap` | | ad pop-ups to tap away, and where to tap |
| `bar_close_fraction` | 0.75 | local copy: see above |
| `still_before_close` | 3 | seconds without video motion that mean an ad has ended |
| `no_exit_seconds` | 20 | an ad that has ended and shows nothing to tap gets Back after this long |
| `skip_after_seconds` | 2 | skip buttons are tapped from this many seconds in |
| `misc_buttons` | | the game's own pop-ups and what to do with each (set by `grab --misc`) |
| `misc_check_seconds`, `misc_max_per_minute` | 5, 8 | watch mode: how often to check for them; most dealt with a minute |
| `near_miss_looks` | 3 | looks in a row a near-miss must sit in the same spot before it is tapped |
| `finished_min_seconds` | | local copy: see above |
| `answer_templates`, `max_answer_taps` | , 12 | local copy: see above |
| `skip_templates` | | local copy: see above |

### New commands and flags
- Commands: `keepalive`, `tidy`, `stats`.
- Global flags: `-v`, `--feed DEVICE`, `--screen raw|png|auto`, `--no-fast-taps`, `--keep-awake` / `--no-keep-awake`.
- `grab --popup`, `grab --skip`, `grab --misc --action`, `tidy --yes`, `keepalive --wait-hours`.
- `.gitignore` now leaves out `logs/` and `control/`.
