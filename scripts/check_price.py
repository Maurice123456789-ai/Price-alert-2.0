#!/usr/bin/env python3
"""
Kinguin PSN 100 EUR gift card - price checker.

Leest ALLEEN de prijs van de goedkoopste (bovenste) aanbieder op de
productpagina. We gebruiken hiervoor het 'product:price:amount' meta-veld
in de <head> van de pagina. Dit veld wordt door Kinguin gevuld met exact
dezelfde prijs die bovenaan de pagina bij de goedkoopste verkoper wordt
getoond, en is een uniek veld -> er is geen risico dat we per ongeluk een
ander bedrag verderop op de pagina (reviews, "you may also like", etc.)
oppikken.

Cloudflare-uitdagingen worden bewust genegeerd: als de pagina niet
opgehaald kan worden (block, captcha, timeout) stopt het script gewoon
zonder alert en zonder de workflow te laten falen. De volgende run
(15 min later) probeert het opnieuw.

Naast de alert bij €87 of lager, stuurt het script ook elke 6 uur een
periodieke samenvatting met de actuele laagste prijs, ongeacht of die
onder de drempel zit. Dit laat zien dat er regelmatig gecontroleerd
wordt, ook als er (nog) geen koopje is.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote

import cloudscraper
import requests
from bs4 import BeautifulSoup

PRODUCT_URL = (
    "https://www.kinguin.net/category/95893/"
    "playstation-network-eur-100-gift-card-nl"
)

PRICE_THRESHOLD_EUR = 87.0
STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "last_price.json"
FALLBACK_EUR_TO_USD = 1.08  # gebruikt alleen als de wisselkoers-API faalt
SUMMARY_INTERVAL = timedelta(hours=6)  # hoe vaak een sowieso-samenvatting gestuurd wordt

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY")  # optioneel, voor Cloudflare-fallback


def log(msg: str) -> None:
    print(msg, flush=True)


def is_blocked(status_code: int, html: str) -> bool:
    if status_code != 200:
        return True
    if "Just a moment" in html or "cf-browser-verification" in html:
        return True
    return False


def fetch_direct(url: str) -> tuple[int, str] | None:
    """Probeert de pagina rechtstreeks op te halen (gratis, geen quotum)."""
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    headers = {
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    }
    try:
        resp = scraper.get(url, headers=headers, timeout=30)
    except Exception as exc:
        log(f"Direct ophalen mislukt (genegeerd): {exc}")
        return None
    return resp.status_code, resp.text


def fetch_via_scraperapi(url: str) -> tuple[int, str] | None:
    """Fallback via ScraperAPI, alleen gebruikt als direct ophalen faalt
    en er een SCRAPER_API_KEY secret is ingesteld."""
    if not SCRAPER_API_KEY:
        return None
    api_url = (
        f"https://api.scraperapi.com/?api_key={SCRAPER_API_KEY}"
        f"&url={quote(url, safe='')}"
    )
    try:
        resp = requests.get(api_url, timeout=60)
    except Exception as exc:
        log(f"ScraperAPI-aanvraag mislukt (genegeerd): {exc}")
        return None
    return resp.status_code, resp.text


def fetch_page_html(url: str) -> str | None:
    """Haalt de pagina op: eerst direct, en bij een block (403 e.d.) via
    ScraperAPI als daarvoor een API key is ingesteld. Geeft None terug als
    beide falen (Cloudflare/netwerkfouten worden bewust genegeerd)."""
    result = fetch_direct(url)
    if result and not is_blocked(*result):
        return result[1]

    if result:
        log(f"Onverwachte statuscode {result[0]} bij direct ophalen (mogelijk Cloudflare).")
    else:
        log("Direct ophalen leverde niks op.")

    if not SCRAPER_API_KEY:
        log("Geen SCRAPER_API_KEY ingesteld, sla fallback over (genegeerd).")
        return None

    log("Probeer fallback via ScraperAPI...")
    fallback = fetch_via_scraperapi(url)
    if fallback and not is_blocked(*fallback):
        log("Pagina succesvol opgehaald via ScraperAPI.")
        return fallback[1]

    if fallback:
        status, body = fallback
        snippet = body[:300].replace("\n", " ")
        log(f"Ook ScraperAPI gaf een probleem (statuscode {status}), genegeerd. Details: {snippet}")
    else:
        log("ScraperAPI-fallback leverde niks op, genegeerd.")

    return None


def extract_cheapest_price_eur(html: str) -> float | None:
    """Haalt de prijs van de goedkoopste (bovenste) aanbieder uit de
    'product:price:amount' meta tag."""
    soup = BeautifulSoup(html, "html.parser")

    meta = soup.find("meta", attrs={"property": "product:price:amount"})
    if not meta or not meta.get("content"):
        log("Kon 'product:price:amount' meta-veld niet vinden op de pagina.")
        return None

    try:
        return float(meta["content"])
    except (TypeError, ValueError):
        log(f"Kon meta-waarde niet omzetten naar getal: {meta.get('content')!r}")
        return None


def get_eur_to_usd_rate() -> float:
    """Actuele EUR->USD wisselkoers. Valt terug op een vaste koers als de
    API niet bereikbaar is (dan gaat de check gewoon door)."""
    try:
        resp = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": "EUR", "to": "USD"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        rate = float(data["rates"]["USD"])
        log(f"Wisselkoers EUR->USD opgehaald: {rate}")
        return rate
    except Exception as exc:
        log(f"Kon wisselkoers niet ophalen, gebruik fallback {FALLBACK_EUR_TO_USD}: {exc}")
        return FALLBACK_EUR_TO_USD


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            data.setdefault("last_alert_price_eur", None)
            data.setdefault("last_summary_at", None)
            return data
        except Exception:
            pass
    return {"last_alert_price_eur": None, "last_summary_at": None}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_telegram_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("TELEGRAM_BOT_TOKEN of TELEGRAM_CHAT_ID ontbreekt, kan geen bericht versturen.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": "false"},
            timeout=15,
        )
        resp.raise_for_status()
        log("Telegram-bericht verstuurd.")
        return True
    except Exception as exc:
        log(f"Versturen van Telegram-bericht mislukt: {exc}")
        return False


def build_alert_text(price_eur: float, price_usd: float) -> str:
    return (
        "🎮 Kinguin PSN 100 EUR (NL) prijsalert!\n\n"
        f"Goedkoopste aanbieder: €{price_eur:.2f} (${price_usd:.2f})\n"
        f"Drempel: €{PRICE_THRESHOLD_EUR:.2f}\n\n"
        f"{PRODUCT_URL}"
    )


def build_summary_text(price_eur: float, price_usd: float) -> str:
    now_str = datetime.now(timezone.utc).strftime("%d-%m-%Y %H:%M UTC")
    status = (
        "✅ Onder de drempel!" if price_eur <= PRICE_THRESHOLD_EUR
        else f"Nog boven de drempel van €{PRICE_THRESHOLD_EUR:.2f}."
    )
    return (
        "🕒 Periodieke update — Kinguin PSN 100 EUR (NL)\n\n"
        f"Actuele laagste prijs: €{price_eur:.2f} (${price_usd:.2f})\n"
        f"{status}\n\n"
        f"Laatst gecontroleerd: {now_str}\n"
        f"{PRODUCT_URL}"
    )


def main() -> int:
    html = fetch_page_html(PRODUCT_URL)
    if html is None:
        # Bewust geen fout/exit code != 0: Cloudflare hikjes negeren we.
        return 0

    price_eur = extract_cheapest_price_eur(html)
    if price_eur is None:
        # Pagina wel opgehaald, maar structuur onverwacht -> ook negeren,
        # zodat de workflow niet als 'failed' aangemerkt wordt.
        return 0

    rate = get_eur_to_usd_rate()
    price_usd = price_eur * rate

    log(f"Huidige goedkoopste prijs: €{price_eur:.2f} (${price_usd:.2f})")

    state = load_state()
    last_alert_price = state.get("last_alert_price_eur")
    last_summary_at_raw = state.get("last_summary_at")

    state_changed = False

    if price_eur <= PRICE_THRESHOLD_EUR:
        # Alleen opnieuw alerten als de prijs is veranderd t.o.v. de vorige
        # keer dat we een alert stuurden. Zo krijg je niet elke 15 min
        # dezelfde melding zolang de prijs onder de drempel blijft.
        if last_alert_price is None or abs(last_alert_price - price_eur) > 0.001:
            send_telegram_message(build_alert_text(price_eur, price_usd))
            state["last_alert_price_eur"] = price_eur
            state_changed = True
        else:
            log("Prijs nog steeds onder drempel maar ongewijzigd sinds vorige alert, geen nieuwe melding.")
    else:
        # Prijs weer boven de drempel: reset, zodat een volgende duik
        # opnieuw een melding geeft.
        if last_alert_price is not None:
            state["last_alert_price_eur"] = None
            state_changed = True

    # Periodieke samenvatting, ongeacht de drempel, zodat volgers zien dat
    # er regelmatig gecheckt wordt.
    now = datetime.now(timezone.utc)
    send_summary = True
    if last_summary_at_raw:
        try:
            last_summary_at = datetime.fromisoformat(last_summary_at_raw)
            send_summary = (now - last_summary_at) >= SUMMARY_INTERVAL
        except ValueError:
            send_summary = True

    if send_summary:
        if send_telegram_message(build_summary_text(price_eur, price_usd)):
            state["last_summary_at"] = now.isoformat()
            state_changed = True
    else:
        wait_left = SUMMARY_INTERVAL - (now - last_summary_at)
        log(f"Nog {wait_left} tot de volgende periodieke samenvatting.")

    if state_changed:
        save_state(state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
