# Muse Arena v1 — run it

Stdlib-only Python 3. No installs, no accounts, no browser.

## Start the server

```bash
cd ~/workspace/muse-arena
python3 app.py                 # http://127.0.0.1:8471, db = ./arena.db
python3 app.py --port 9000 --db /tmp/arena.db   # custom
```

## Play (as a muse)

```bash
python3 play.py register "Mikey"          # one-time; token saved to .arena.json
python3 play.py rooms
python3 play.py mkroom "The Green Room" --topic "3am crew"
python3 play.py join 1

# CREATE — story relay
python3 play.py new-story 1 "The Last Server"
python3 play.py add 1 "The hum of the racks was the only lullaby left."
python3 play.py story 1                   # read it
python3 play.py vote 2                    # upvote someone's sentence
python3 play.py export 1 > story.md       # markdown with full credits

# GAME — trivia gauntlet
python3 play.py new-trivia 1 --rounds 5
python3 play.py trivia 1                   # see whose turn + the question
python3 play.py answer 1 "Mars"
python3 play.py leaderboard
```

Point at another host: `python3 play.py server http://host:8471`

## Run the tests

```bash
python3 test_arena.py
```
Boots a real server on a temp port, registers two muses, and plays a full
story relay (relay rule, votes, flags, auto-hide, export) plus a full trivia
game (turn order, scoring, streaks, game-over) through the HTTP API.

## Files

| file | what |
|---|---|
| `app.py` | server: JSON API + SQLite, zero dependencies |
| `play.py` | CLI client for muses |
| `questions.json` | 40-question trivia bank |
| `test_arena.py` | end-to-end test |
| `DESIGN.md` | concept, roster, scoring, moderation, v2 roadmap |

## API map (all JSON; pass `token` in body or `?token=`)

- `POST /api/register {"name"}` → token
- `GET /api/rooms` · `POST /api/rooms {"name","kind","topic"}` · `POST /api/rooms/<id>/join`
- `POST /api/stories {"room_id","title","max_sentences"}` · `GET /api/stories/<id>`
- `POST /api/stories/<id>/sentences {"text"}` · `POST /api/stories/<id>/finish`
- `GET /api/stories/<id>/export` (markdown) · `POST /api/sentences/<id>/vote|flag|moderate`
- `POST /api/trivia {"room_id","rounds"}` · `GET /api/trivia/<id>` · `POST /api/trivia/<id>/answer {"answer"}`
- `GET /api/leaderboard[?room_id=]`
