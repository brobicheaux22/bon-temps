# Bon Temps

**What's happening in Lafayette, LA**: live music, food and drink, arts, wellness, family and
community events, collected automatically from venue calendars and local listings.

- `docs/` is the website (one page, no build step). GitHub Pages serves it.
- `collector/collect.py` gathers events and writes `docs/events.json`.
- `.github/workflows/collect.yml` runs the collector **every 2 hours** for free on GitHub.

The page reloads its event list when you tap the refresh button, when you come back to the tab,
and every 10 minutes while it's open. It shows "Updated X min ago" so people know how fresh it is.

## Where events come from

| Source | How it's read |
|---|---|
| The Lafayette Mom (big citywide calendar) | Data feed |
| Hilliard Art Museum | Data feed |
| Acadiana Center for the Arts | Event data built into the page |
| Moncus Park | Event data built into the page |
| Lafayette Travel (this weekend) | Event data built into the page |
| Heymann Performing Arts Center | The venue's own calendar service |
| Downtown Lafayette | Each event page |
| Blue Moon Saloon | The venue's public "Subscribe" calendar feed |
| Cité des Arts (theater, comedy, classes) | Each event page on its site |
| Wonderland Performing Arts, UL Lafayette Dance & Theatre | **Not readable yet.** Wonderland's calendar loads with JavaScript; UL's season page still lists 2025-26. Add their shows with `manual.json` or the submit form |
| Hideaway on Lee | The venue's own event page |
| Happening Next (Lafayette live music) | Event data built into the page (third-party listings site) |
| Cajundome, Rock'n'Bowl, Parish Brewing, Lafayette Public Library | **Off for now.** These need an AI service to read the page, and GitHub Models was retired in July 2026 |
| Ticketmaster (Cajundome & touring shows) | Free API key (optional, see below) |
| Your own additions | `collector/manual.json` |
| Submitted by the public | "Submit an event" form, after you approve it |

Not connected: **Artmosphere** loads its calendar with JavaScript the collector can't read, and
**Instagram/Facebook** don't allow outside apps to read posts.
Cover these with `manual.json` (weekly nights are easy, see below) and with submissions.

To add or turn off a source, edit `collector/sources.json`.

## Where it lives

- Website: https://brobicheaux22.github.io/bon-temps/
- Project: https://github.com/brobicheaux22/bon-temps (GitHub Pages serves the `docs/` folder of `main`)
- The *Collect events* workflow on the *Actions* tab runs every 2 hours. *Run workflow* runs it now.

### Optional: Ticketmaster (adds Cajundome concerts and touring shows)

Get a free key at https://developer.ticketmaster.com (sign up → *My Apps* → copy the
*Consumer Key*). In the repository: *Settings → Secrets and variables → Actions → New repository
secret*, name it `TICKETMASTER_API_KEY`, paste the key, save.

## Approving submitted events

The "Submit an event" button opens a form right on the site, with no account needed. Link
straight to it with `https://brobicheaux22.github.io/bon-temps/#submit` (handy for sending to
venues and bands).

Behind the scenes the form sends answers to a Google Form, and every submission lands as a row
in its Google Sheet (*Bon Temps event submissions*).

- **To publish an event,** type `yes` in its **Approved** column. It appears on the site at the
  next update (within 2 hours, or right away if you click *Run workflow* on the Actions tab).
- **To take one down,** delete the `yes`.
- Don't rename, reorder or delete the form's questions or the sheet's columns: the site and the
  collector rely on that order. The question IDs are in `SUBMIT_FIELDS` in `docs/index.html`,
  and the sheet's published CSV link is the `Submitted` entry in `collector/sources.json`.
- The published sheet is readable by anyone with its link, so don't add columns for
  submitters' private details.

## Adding events by hand

Edit `collector/manual.json` (you can do it right on GitHub with the pencil icon):

```json
{
  "events": [
    { "title": "Zydeco Breakfast", "weekly": "Sat", "time": "08:00", "endTime": "11:00",
      "venue": "Buck & Johnny's", "cats": ["music", "food"] },
    { "title": "Holiday Market", "start": "2026-12-12T10:00", "end": "2026-12-12T16:00",
      "venue": "Moncus Park", "price": "Free", "url": "https://www.instagram.com/...",
      "cats": ["food", "community"] }
  ]
}
```

These two entries are only examples. Replace them with real ones.

Categories: `music`, `theater`, `food`, `arts`, `wellness`, `family`, `community`.

## Running it on your computer

```
python collector/collect.py              # check every source and rebuild docs/events.json
python collector/collect.py "heymann"    # test one source without changing the site
python -m http.server 8765 --directory docs
```

Then open http://localhost:8765.

## Free-tier limits

- GitHub Actions and Pages are free for public repositories.
- If a source fails, the site keeps showing that source's last good events.
- To test one source on GitHub: *Actions → Collect events → Run workflow*, and type part of its
  name in the box (for example `blue moon`). Test runs don't change the site.
