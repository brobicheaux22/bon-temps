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

## Put it online (one time, about 10 minutes)

1. **Create the repository.** Go to https://github.com/new, name it `bon-temps`, make it
   **Public**, and click *Create repository*. (If you pick a different name, change `REPO` near the
   top of the script in `docs/index.html`.)
2. **Upload the files.** The easiest way is [GitHub Desktop](https://desktop.github.com):
   *File → Add local repository →* choose the `bon-temps` folder → *create a repository* →
   *Publish repository*. Or, on the repository page, use *Add file → Upload files* and drag in
   everything inside the folder, including the `.github` folder.
3. **Turn on the website.** In the repository: *Settings → Pages → Build and deployment →
   Source: Deploy from a branch → Branch: `main`, folder `/docs` → Save.* In a minute or two
   the site is live at `https://brobicheaux22.github.io/bon-temps/`.
4. **Make two labels** for submissions: *Issues → Labels → New label*, named `submission`
   and `approved`.
5. **Run the collector once.** *Actions* tab → *Collect events* → *Run workflow*. After that it
   runs by itself every 2 hours. This first run is also when the AI page reading turns on.

### Optional: Ticketmaster (adds Cajundome concerts and touring shows)

Get a free key at https://developer.ticketmaster.com (sign up → *My Apps* → copy the
*Consumer Key*). In the repository: *Settings → Secrets and variables → Actions → New repository
secret*, name it `TICKETMASTER_API_KEY`, paste the key, save.

## Approving submitted events

The "Submit an event" button opens a short form on GitHub (submitters need a free GitHub
account). Each submission becomes an issue labeled `submission`. To publish it, add the
`approved` label. The collector runs right away and the event shows up within a few minutes.
Close the issue once the event is over.

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

Categories: `music`, `food`, `arts`, `wellness`, `family`, `community`.

## Running it on your computer

```
python collector/collect.py              # check every source and rebuild docs/events.json
python collector/collect.py "heymann"    # test one source without changing the site
python -m http.server 8765 --directory docs
```

Then open http://localhost:8765. The AI-read pages and submissions only run on GitHub, unless
you set `GITHUB_TOKEN` to a GitHub token that has the *Models* permission.

## Free-tier limits

- GitHub Actions and Pages are free for public repositories.
- If a source fails, the site keeps showing that source's last good events.
- To test one source on GitHub: *Actions → Collect events → Run workflow*, and type part of its
  name in the box (for example `blue moon`). Test runs don't change the site.
