# meta description: Melody — одна команда .music (песня — автор): карточка с кнопкой-ссылкой на песню и караоке-анимацией текста
# meta developer: @unkxe

__version__ = (4, 0, 0)

import asyncio
import bisect
import functools
import html
import json
import re
import time
import urllib.parse
import urllib.request

from .. import loader, utils

DEEZER_SEARCH = "https://api.deezer.com/search?limit=10&q="
DEEZER_PAGE = "https://www.deezer.com/track/{}"
SONG_LINK_D = "https://song.link/d/{}"
SONG_LINK_I = "https://song.link/i/{}"
ITUNES_SEARCH = "https://itunes.apple.com/search?media=music&entity=song&limit=10&term="
LRCLIB_GET = "https://lrclib.net/api/get?"
LRCLIB_SEARCH = "https://lrclib.net/api/search?"
LYRICS_OVH = "https://api.lyrics.ovh/v1/{}/{}"
PIPED = [
    "https://api.piped.private.coffee",
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.drgns.space",
    "https://pipedapi.phoenixthrush.com",
    "https://pipedapi.ducks.party",
    "https://pipedapi.reallyaweso.me",
    "https://pipedapi.leptons.xyz",
    "https://pipedapi.adminforge.de",
]
INV = [
    "https://inv.nadeko.net",
    "https://invidious.nerdvpn.de",
    "https://yewtu.be",
    "https://invidious.f5.si",
    "https://iv.datura.network",
]
UA_API = "AtaraxiaMelody/2.2 (Telegram userbot; contact https://t.me/unkxe)"
UA_PAGE = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
LRC_STAMP = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:\.(\d{1,3}))?\]\s*")
JUNK = re.compile(r"\[[^\]]*\]|<[^>]*>")
WORD = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
SPLIT = re.compile(r"\s+[-—–]\s+|\s*\|\s*|\s*,\s+")
STRONG = 1.2
OK_YT = 0.5
GAP_ANIM = 8.0
CNT_MAX = 8

# кандидат: (title, artist, album, id_or_url, score, ms, kind)
DEE, YTB = "deezer", "youtube"


@loader.tds
class Melody(loader.Module):
    """Ищет песню и держит её караоке в живой карточке: ссылка + текст"""

    strings = {"name": "Melody"}

    def __init__(self):
        self.config = loader.ModuleConfig(
            loader.ConfigValue("lines", 3, lambda: "Строк текста до и после текущей (3+3)"),
            loader.ConfigValue("offset", 0.0, lambda: "Сдвиг таймингов в секундах"),
            loader.ConfigValue("plain_speed", 3.5, lambda: "Секунд на строку без синхронизации"),
        )

    async def client_ready(self, client, db):
        self._client = client
        self._token = 0
        self._started = 0.0
        self._form = None
        self._anim = None

    @loader.command()
    async def music(self, message):
        """<песня> [- автор] — карточка песни: караоке, кнопка-ссылка и закрыть"""
        raw = (utils.get_args_raw(message) or "").strip()
        if not raw:
            return await utils.answer(
                message, "🎛 Пример: .music целую тебя — капсайз (автор не обязателен)"
            )
        title, artist = self._split_args(raw)

        await self._stop_current()
        self._token += 1
        token = self._token

        song = await self._resolve_track(raw, title, artist)
        if not song:
            return await utils.answer(
                message, "🚫 Не нашёл «{}» ни в одном источнике. Уточни название".format(raw)
            )

        song["timeline"] = []

        try:
            form = await self.inline.form(
                "",
                message=message,
                rich_html=self._screen(song, 0, state="wait"),
                force_me=True,
            )
        except Exception as e:
            return await utils.answer(message, "⚠️ Карточка не поднялась: {!r}".format(e))

        self._form = form
        self._anim = asyncio.ensure_future(
            self._run(token, form, song, title, artist, raw)
        )

    # ── живой цикл: ищем текст, потом крутим караоке ─────────────

    async def _run(self, token, form, song, title, artist, raw):
        try:
            item = await self._fetch_lyrics(song, title, artist, raw)
            if self._token != token or not self._form:
                return
            song["timeline"] = self._build_timeline(item, song) if item else []
            if not song["timeline"]:
                await self._edit(form, self._screen(song, 0, state="notext"))
                return
            await self._edit(form, self._screen(song, 0, state="play"))
            if self._token != token or not self._form:
                return
            self._started = time.monotonic()
            await self._karaoke(token, form, song)
        except asyncio.CancelledError:
            pass
        except Exception:
            await self._edit(form, self._screen(song, 0, state="notext"))

    async def _karaoke(self, token, form, song):
        tl = song["timeline"]
        if not tl:
            return
        synced = bool(song.get("synced"))
        times = [e["t"] for e in tl]
        last = len(tl) - 1
        thr = GAP_ANIM
        shown = None
        fails = 0
        while self._token == token and self._form:
            elapsed = time.monotonic() - self._started + self._opt("offset", float, 0.0)
            if elapsed >= times[last] + 4.0:
                await self._edit(form, self._screen(song, last, state="end"))
                return
            frame = None
            if synced:
                j = bisect.bisect_right(times, elapsed)
                if j <= last:
                    gap = times[j] - (times[j - 1] if j > 0 else 0.0)
                    left = times[j] - elapsed
                    if gap >= thr and 0 < left <= gap:
                        if left > CNT_MAX:
                            frame = ("hold", j)
                        elif left > 0.05:
                            frame = ("cnt", j, int(left - 0.05) + 1)
            if frame is None:
                i = max(0, min(bisect.bisect_right(times, elapsed) - 1, last))
                frame = ("line", i)
            if frame != shown:
                shown = frame
                if frame[0] == "cnt":
                    ok = await self._edit(
                        form, self._screen(song, frame[1], state="count", n=frame[2])
                    )
                elif frame[0] == "hold":
                    ok = await self._edit(
                        form, self._screen(song, frame[1], state="count")
                    )
                else:
                    ok = await self._edit(
                        form, self._screen(song, frame[1], state="play")
                    )
                if ok:
                    fails = 0
                else:
                    fails += 1
                    if fails >= 6:
                        return
            await asyncio.sleep(0.35)

    async def _edit(self, form, screen):
        try:
            await form.edit(rich_html=screen)
            return True
        except Exception as e:
            wait = getattr(e, "seconds", 0)
            if wait:
                await asyncio.sleep(min(float(wait), 8.0))
            return False

    # ── карточка ─────────────────────────────────────────────────

    def _screen(self, song, idx, state="play", n=None):
        k = self.inline.screen()
        title = html.escape(str(song["title"]))
        artist = html.escape(str(song["artist"]))
        album = html.escape(str(song.get("album") or ""))
        url = html.escape(str(song["url"]), quote=True)

        k.add("<h3>🎧 {}</h3>".format(title))
        k.add("<i>{} · {}</i>".format(artist, album) if album else "<i>{}</i>".format(artist))
        k.add("⎯" * 25)
        k.add(self._lyrics_block(song, idx, state=state, n=n))
        k.add("⎯" * 25)
        k.add('<tg-button type="url" url="{}">🔗 Слушать песню</tg-button>'.format(url))
        k.add(k.row(k.tap("🚫 Закрыть", self._close_cb, style="danger", force_me=True)))
        if state == "wait":
            k.add("<i>🔎 Ищу текст в открытых источниках…</i>")
        elif state == "count":
            k.add("<i>⏳ Инструментальная пауза — отсчёт до следующей строки</i>")
        elif state == "notext":
            k.add("<i>🌑 Текст не нашёлся — слушай по ссылке</i>")
        elif state == "end":
            k.add("<i>🏁 Песня закончилась</i>")
        else:
            k.add("<i>🎤 Жирная строка — её поют сейчас</i>")
        return k

    def _lyrics_block(self, song, idx, state="play", n=None):
        tl = song["timeline"]
        if not tl:
            return "<blockquote><i>текст появится, когда найду</i></blockquote>"
        win = max(1, min(6, self._opt("lines", int, 3)))
        idx = max(0, min(idx, len(tl) - 1))
        link = html.escape(str(song["url"]), quote=True)

        def blue(e):
            return '<a href="{}">{}</a>'.format(link, html.escape(e["text"]))

        if state == "count":
            rows = [blue(e) for e in tl[max(0, idx - win):idx]]
            rows.append("<b>⏳ {}</b>".format(n) if n else "<b>⏳</b>")
            rows += [blue(e) for e in tl[idx:idx + win]]
            return "<blockquote>" + "<br>".join(rows) + "</blockquote>"

        rows = [blue(e) for e in tl[max(0, idx - win):idx]]
        rows.append("<b>🎤 {}</b>".format(html.escape(tl[idx]["text"])))
        rows += [blue(e) for e in tl[idx + 1:idx + 1 + win]]
        return "<blockquote>" + "<br>".join(rows) + "</blockquote>"

    async def _close_cb(self, call):
        try:
            await call.answer("🚫 Останавливаю")
        except Exception:
            pass
        await self._stop_current()

    async def _stop_current(self):
        self._token += 1
        task, form = self._anim, self._form
        self._anim = None
        self._form = None
        if task:
            task.cancel()
        if form:
            try:
                await form.delete()
            except Exception:
                pass

    # ── аргументы: песня — автор ─────────────────────────────────

    def _split_args(self, raw):
        parts = SPLIT.split(raw, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return parts[0].strip(), parts[1].strip()
        return raw.strip().strip('«»"\''), ""

    # ── поиск трека: deezer → youtube → itunes ───────────────────

    async def _resolve_track(self, raw, title, artist):
        m = re.search(r"song\.link/(d|i)/(\d+)", raw) or re.search(
            r"music\.apple\.com/[^\s]*[?&]i=(\d+)", raw
        )
        if m:
            pref = "d" if (m.lastindex or 0) > 1 and m.group(1) == "d" else "i"
            song = await self._from_songlink(pref, m.groups()[-1])
            if song:
                return song

        quick = (title + " " + artist).strip() if artist else raw

        dbest = await self._best_of(
            self._pools_deezer(quick, title, artist, raw), title, artist
        )
        if dbest and dbest[4] >= STRONG:
            return self._as_song(dbest)

        ybest = await self._best_of(
            self._pools_youtube(quick, title, artist, raw), title, artist
        )
        if ybest and ybest[4] >= STRONG:
            return self._as_song(ybest)

        best = dbest if not ybest or (dbest and dbest[4] >= ybest[4]) else ybest
        if best and best[4] >= OK_YT:
            return self._as_song(best)

        return await self._itunes_pick(title, artist, raw)

    async def _best_of(self, gen, title, artist):
        best = None
        async for pool in gen:
            for cand in pool:
                score = self._score_cand(cand, title, artist)
                cand[4] = score
                if not best or score > best[4]:
                    best = cand
            if best and best[4] >= STRONG:
                break
        return best

    async def _pools_deezer(self, quick, title, artist, raw):
        for q in ([quick, title] if artist else self._variants(raw)):
            data = await self._api(DEEZER_SEARCH + urllib.parse.quote(q))
            pool = []
            for t in (data or {}).get("data") or []:
                if not (t.get("title") and t.get("id")):
                    continue
                ct = t.get("title")
                ca = (t.get("artist") or {}).get("name") or ""
                pool.append([
                    ct,
                    ca,
                    (t.get("album") or {}).get("title") or "",
                    str(t.get("id")),
                    0.0,
                    int(t.get("duration") or 0) * 1000,
                    DEE,
                ])
            if pool:
                yield pool

    async def _pools_youtube(self, quick, title, artist, raw):
        for q in ([quick, title] if artist else [raw]):
            found = await self._youtube_engine(q)
            if found:
                yield found

    async def _youtube_engine(self, q):
        tasks = [
            asyncio.ensure_future(self._piped_once(base, q)) for base in PIPED
        ] + [asyncio.ensure_future(self._inv_once(base, q)) for base in INV]
        try:
            for fut in asyncio.as_completed(tasks):
                pool = await fut
                if pool:
                    return pool
        except Exception:
            pass
        return None

    async def _piped_once(self, base, query):
        for flt in ("music_songs", "videos"):
            pool = []
            data = await self._api(
                base + "/search?filter=" + flt + "&q=" + urllib.parse.quote(query), 9
            )
            for it in (data or {}).get("items") or []:
                u = it.get("url") or ""
                if "/watch" not in u:
                    continue
                pool.append([
                    it.get("title") or "",
                    (it.get("uploaderName") or "").replace(" - Topic", "").strip(),
                    "",
                    "https://youtube.com" + u,
                    0.0,
                    int(it.get("duration") or 0) * 1000,
                    YTB,
                ])
            if pool:
                return pool
        return []

    async def _inv_once(self, base, query):
        data = await self._api(
            base + "/api/v1/search?type=video&q=" + urllib.parse.quote(query), 9
        )
        pool = []
        for it in data if isinstance(data, list) else []:
            vid = it.get("videoId") or ""
            if not vid:
                continue
            pool.append([
                (it.get("title") or "").strip(),
                (it.get("author") or "").replace(" - Topic", "").strip(),
                "",
                "https://youtube.com/watch?v=" + vid,
                0.0,
                int(it.get("lengthSeconds") or 0) * 1000,
                YTB,
            ])
        if pool:
            return pool
        return []

    def _score_cand(self, cand, title, artist):
        ct, ca = cand[0], cand[1]
        title_l, artist_l = (title or "").lower(), (artist or "").lower()
        tts = set(self._tokens(title_l))
        ats = set(self._tokens(artist_l))
        cts = set(self._tokens(ct))
        cas = set(self._tokens(ca))
        want = tts | ats
        have = cts | cas
        if not want or not have:
            return 0.0
        cov = len(want & have) / len(want)
        pen = 0.05 * len(have - want) / len(have)
        order = self._lcs(self._tokens(title_l), self._tokens(ct)) / max(
            1, len(self._tokens(title_l))
        )
        phrase = 0.25 if title_l and title_l in ct.lower() else 0.0
        art = (
            0.15
            if not artist_l
            else (0.15 if artist_l in ca.lower() or ca.lower() in artist_l else 0.0)
        )
        return cov - pen + 0.15 * order + phrase + art

    def _as_song(self, cand):
        t, a, alb, ident, sc, ms, kind = cand
        if kind == DEE:
            return {
                "title": t,
                "artist": a,
                "album": alb,
                "url": SONG_LINK_D.format(ident),
                "ms": ms,
            }
        return {"title": t, "artist": a, "album": alb, "url": ident, "ms": ms}

    async def _itunes_pick(self, title, artist, raw):
        for q in ([title + " " + artist, title] if artist else self._variants(raw)):
            et = urllib.parse.quote(q)
            for extra in ("", "&country=ru"):
                data = await self._api(ITUNES_SEARCH + et + extra)
                tracks = [
                    r for r in (data or {}).get("results", []) if r.get("kind") == "track"
                ]
                if tracks:
                    tracks.sort(
                        key=lambda t: self._score_it(t, title, artist), reverse=True
                    )
                    t = tracks[0]
                    page = await self._from_songlink("i", str(t.get("trackId") or ""))
                    if page:
                        page["album"] = t.get("collectionName") or ""
                        return page
                    return {
                        "title": t.get("trackName") or "Трек",
                        "artist": t.get("artistName") or "Исполнитель",
                        "album": t.get("collectionName") or "",
                        "url": SONG_LINK_I.format(t.get("trackId", "")),
                        "ms": int(t.get("trackTimeMillis") or 0),
                    }
        return None

    def _score_it(self, t, title, artist):
        return self._score_cand(
            [
                t.get("trackName") or "",
                t.get("artistName") or "",
                "",
                "",
                0.0,
                0,
                "itunes",
            ],
            title,
            artist,
        )

    async def _from_songlink(self, pref, pid):
        pid = str(pid or "")
        if not pid.isdigit():
            return None
        page = await self._page("https://song.link/{}/{}".format(pref, pid))
        m = NEXT_DATA.search(page or "")
        if not m:
            return None
        try:
            ent = json.loads(m.group(1))["props"]["pageProps"]["pageData"]["entityData"]
            if not ent.get("title"):
                return None
            return {
                "title": str(ent["title"]),
                "artist": str(ent.get("artistName") or "Исполнитель"),
                "album": "",
                "url": "https://song.link/{}/{}".format(pref, pid),
                "ms": int(ent.get("duration") or 0),
            }
        except Exception:
            return None

    def _variants(self, query):
        words = query.split()
        out, seen = [], set()
        cands = [query]
        if len(words) > 2:
            cands += [
                " ".join(words[1:]),
                " ".join(words[:-1]),
                " ".join(words[:3]),
                " ".join(words[-2:]),
            ]
        elif len(words) == 2:
            cands += [" ".join(words[1:]), " ".join(words[:1])]
        for c in cands:
            c = c.strip().strip('«»"\'')
            if c and c.lower() not in seen:
                seen.add(c.lower())
                out.append(c)
        return out

    def _tokens(self, s):
        return [w.lower() for w in WORD.findall(s or "")]

    def _lcs(self, a, b):
        m = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
        best = 0
        for i in range(1, len(a) + 1):
            for j in range(1, len(b) + 1):
                if a[i - 1] == b[j - 1]:
                    m[i][j] = m[i - 1][j - 1] + 1
                    best = max(best, m[i][j])
                else:
                    m[i][j] = max(m[i - 1][j], m[i][j - 1])
        return best

    # ── текст песни: открытые источники ──────────────────────────

    async def _fetch_lyrics(self, song, title, artist, raw):
        st, sa = song.get("title") or "", song.get("artist") or ""
        pairs = []
        if artist:
            pairs.append((artist, title or st))
        if st:
            pairs.append((sa, st))
        for a, t in pairs:
            aq, tq = urllib.parse.quote(a or ""), urllib.parse.quote(t)
            for url in (
                LRCLIB_GET + "artist_name=" + aq + "&track_name=" + tq,
                LRCLIB_SEARCH + "track_name=" + tq + "&artist_name=" + aq,
            ):
                got = self._pick(await self._api_retry(url))
                if got:
                    return got
        for q in (raw, title or raw):
            got = self._pick(
                await self._api_retry(LRCLIB_SEARCH + "q=" + urllib.parse.quote(q))
            )
            if got:
                return got
        return await self._lyrics_ovh(sa, st)

    async def _lyrics_ovh(self, artist, title):
        if not artist or not title:
            return None
        url = LYRICS_OVH.format(urllib.parse.quote(artist), urllib.parse.quote(title))
        data = await self._api(url, 12)
        text = ((data or {}).get("lyrics") or "").strip()
        if not text or text.lower().startswith("instrumental") or len(text) < 40:
            return None
        return {"synced": False, "text": text}

    async def _api_retry(self, url, tries=2, delay=1.5):
        for i in range(tries):
            data = await self._api(url)
            if data is not None:
                return data
            await asyncio.sleep(delay)
        return None

    def _pick(self, data):
        if not data:
            return None
        items = data if isinstance(data, list) else [data]
        for it in items:
            if ((it or {}).get("syncedLyrics") or "").strip():
                return {"synced": True, "text": it["syncedLyrics"]}
        for it in items:
            if ((it or {}).get("plainLyrics") or "").strip():
                return {"synced": False, "text": it["plainLyrics"]}
        return None

    def _build_timeline(self, item, song):
        text = (item.get("text") or "").replace("\r", "")
        song["synced"] = False
        if item.get("synced"):
            tl = self._parse_lrc(text)
            if tl:
                song["synced"] = True
                return tl
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines:
            return []
        dur = (song.get("ms") or 0) / 1000.0
        if dur > 0:
            step = min(6.0, max(2.0, dur / len(lines)))
        else:
            step = self._opt("plain_speed", float, 3.5)
        return [{"t": i * step, "text": line} for i, line in enumerate(lines)]

    def _parse_lrc(self, text):
        out = []
        for raw in text.splitlines():
            s = raw.strip()
            stamps = []
            while True:
                m = LRC_STAMP.match(s)
                if not m:
                    break
                mm, ss, frac = m.groups()
                stamps.append(
                    int(mm) * 60 + int(ss) + int(((frac or "0") + "000")[:3]) / 1000.0
                )
                s = s[m.end():]
            phrase = JUNK.sub("", s).strip()
            if not stamps or not phrase:
                continue
            for ts in stamps:
                out.append({"t": ts, "text": phrase})
        out.sort(key=lambda e: e["t"])
        return out

    # ── сеть ─────────────────────────────────────────────────────

    def _opt(self, name, cast, default):
        try:
            return cast(self.config[name])
        except Exception:
            return default

    async def _api(self, url, timeout=8):
        try:
            return await asyncio.get_running_loop().run_in_executor(
                None, functools.partial(self._grab, url, timeout)
            )
        except Exception:
            return None

    def _grab(self, url, timeout=8):
        req = urllib.request.Request(url, headers={"User-Agent": UA_API})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    async def _page(self, url, timeout=12):
        try:
            return await asyncio.get_running_loop().run_in_executor(
                None, functools.partial(self._wget, url, timeout)
            )
        except Exception:
            return None

    def _wget(self, url, timeout=12):
        req = urllib.request.Request(url, headers={"User-Agent": UA_PAGE})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
