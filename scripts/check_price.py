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
COUPON_SOURCE_URL = "https://www.planetkey.de/shops/kinguin"

PRICE_THRESHOLD_EUR = 87.0
STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "last_price.json"
FALLBACK_EUR_TO_USD = 1.08  # gebruikt alleen als de wisselkoers-API faalt
SUMMARY_INTERVAL = timedelta(hours=6)  # hoe vaak een sowieso-samenvatting gestuurd wordt
COUPON_CHECK_INTERVAL = timedelta(hours=24)  # het is een maandcode, dus 1x per dag is genoeg

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


def extract_coupon_code(html: str) -> tuple[str, str] | None:
    """Haalt de huidige Kinguin-kortingscode en het kortingspercentage op
    van planetkey.de, een Duitse prijsvergelijkingssite die de code in
    platte tekst toont (in tegenstelling tot de meeste coupon-sites, die
    'm achter een verplichte klik verstoppen). Geeft (code, percentage)
    terug, of None als er niks gevonden wordt."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    match = re.search(r'(\d{1,2})%\s*Gutscheincode[:\s"]*([A-Z0-9]{4,14})', text)
    if not match:
        log("Kon geen kortingscode vinden op planetkey.de.")
        return None

    percentage, code = match.groups()
    return code, percentage


def get_current_coupon() -> tuple[str, str] | None:
    """Haalt de actuele kortingscode op. Faalt stil (geeft None) bij
    Cloudflare/netwerkproblemen, net als de prijs-check."""
    html = fetch_page_html(COUPON_SOURCE_URL)
    if html is None:
        return None
    return extract_coupon_code(html)


def format_coupon_line(coupon: tuple[str, str] | None) -> str:
    if coupon is None:
        return ""
    code, percentage = coupon
    return f"\n🎟️ Extra korting met code: {code} ({percentage}% erbij!)\n"


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
            data.setdefault("coupon_code", None)
            data.setdefault("coupon_percentage", None)
            data.setdefault("coupon_checked_at", None)
            return data
        except Exception:
            pass
    return {
        "last_alert_price_eur": None,
        "last_summary_at": None,
        "coupon_code": None,
        "coupon_percentage": None,
        "coupon_checked_at": None,
    }


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


def build_alert_text(price_eur: float, price_usd: float, coupon: tuple[str, str] | None) -> str:
    return (
        "🚨🔥 PRIJSKNALLER! 🔥🚨\n\n"
        "Kinguin PSN 100 EUR (NL) tegoedkaart is nú te scoren voor:\n\n"
        f"💶 €{price_eur:.2f}  |  💵 ${price_usd:.2f}\n\n"
        f"Dat is onder onze drempel van €{PRICE_THRESHOLD_EUR:.0f}! Wees er snel bij, "
        "dit soort prijzen zijn zo weer weg 👇\n"
        f"{format_coupon_line(coupon)}\n"
        f"{PRODUCT_URL}"
    )


def build_summary_text(price_eur: float, price_usd: float, coupon: tuple[str, str] | None) -> str:
    if price_eur <= PRICE_THRESHOLD_EUR:
        header = "🔥 6-uurs update: scherpe prijs gespot!"
        status = f"✅ Dit zit onder onze drempel van €{PRICE_THRESHOLD_EUR:.0f} — nu toeslaan dus!"
    else:
        header = "📊 6-uurs update"
        status = f"👀 Nog boven onze sweet spot van €{PRICE_THRESHOLD_EUR:.0f}, we blijven scherp checken."

    return (
        f"{header}\n\n"
        "Elke 6 uur checken we de prijs voor je 👇\n\n"
        "Kinguin PSN 100 EUR (NL) tegoedkaart:\n\n"
        f"💶 €{price_eur:.2f}  |  💵 ${price_usd:.2f}\n\n"
        f"{status}\n"
        f"{format_coupon_line(coupon)}\n"
        f"👉 {PRODUCT_URL}"
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
    coupon_checked_at_raw = state.get("coupon_checked_at")

    state_changed = False
    now = datetime.now(timezone.utc)

    # Kortingscode is een maandcode, dus die hoeft maar 1x per 24 uur
    # opnieuw gecheckt te worden. Tussendoor gebruiken we de laatst
    # bekende code (uit de state) in elk bericht.
    refresh_coupon = True
    if coupon_checked_at_raw:
        try:
            coupon_checked_at = datetime.fromisoformat(coupon_checked_at_raw)
            refresh_coupon = (now - coupon_checked_at) >= COUPON_CHECK_INTERVAL
        except ValueError:
            refresh_coupon = True

    if refresh_coupon:
        fresh_coupon = get_current_coupon()
        if fresh_coupon:
            state["coupon_code"], state["coupon_percentage"] = fresh_coupon
            state["coupon_checked_at"] = now.isoformat()
            state_changed = True
            log(f"Kortingscode ververst: {fresh_coupon[0]} ({fresh_coupon[1]}%)")
        else:
            log("Kon geen nieuwe kortingscode ophalen, gebruik eventueel eerder bekende code.")
    else:
        log("Kortingscode is nog geen 24 uur oud, gebruik gecachte waarde.")

    coupon = (
        (state["coupon_code"], state["coupon_percentage"])
        if state.get("coupon_code")
        else None
    )

    if price_eur <= PRICE_THRESHOLD_EUR:
        # Alleen opnieuw alerten als de prijs is veranderd t.o.v. de vorige
        # keer dat we een alert stuurden. Zo krijg je niet elke 15 min
        # dezelfde melding zolang de prijs onder de drempel blijft.
        if last_alert_price is None or abs(last_alert_price - price_eur) > 0.001:
            send_telegram_message(build_alert_text(price_eur, price_usd, coupon))
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
    send_summary = True
    if last_summary_at_raw:
        try:
            last_summary_at = datetime.fromisoformat(last_summary_at_raw)
            send_summary = (now - last_summary_at) >= SUMMARY_INTERVAL
        except ValueError:
            send_summary = True

    if send_summary:
        if send_telegram_message(build_summary_text(price_eur, price_usd, coupon)):
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
