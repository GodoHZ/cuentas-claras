"""Prueba los gestos en WebKit: deslizar para borrar y tirar para actualizar."""
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
notas = []
with sync_playwright() as p:
    nav = p.chromium.launch()   # WebKit no deja construir eventos Touch a mano
    ctx = nav.new_context(viewport={"width": 390, "height": 844}, is_mobile=True,
                          has_touch=True, locale="es-ES")
    pag = ctx.new_page()
    pag.on("pageerror", lambda e: notas.append("error JS: " + str(e)))
    pag.on("dialog", lambda d: d.accept())
    pag.goto(BASE + "/movimientos", wait_until="networkidle")

    fila = pag.locator(".tx-wrap").first
    texto = fila.inner_text().split("\n")[0]
    # deslizar hacia la izquierda
    pag.evaluate("""() => {
        const el = document.querySelector('.tx-wrap');
        const caja = el.getBoundingClientRect();
        const y = caja.top + caja.height / 2;
        const toque = (tipo, cx) => el.dispatchEvent(new TouchEvent(tipo, {
            bubbles: true, cancelable: true,
            touches: tipo === 'touchend' ? [] : [new Touch({identifier: 1, target: el, clientX: cx, clientY: y})],
            changedTouches: [new Touch({identifier: 1, target: el, clientX: cx, clientY: y})]}));
        toque('touchstart', 300); toque('touchmove', 250); toque('touchmove', 200); toque('touchend', 200);
    }""")
    pag.wait_for_timeout(500)
    notas.append("al deslizar aparece el botón de borrar: " +
                 str(pag.locator(".tx-wrap.abierto").count() == 1))
    pag.screenshot(path="/out/deslizar.png")

    antes = pag.locator(".tx").count()
    pag.locator(".tx-wrap.abierto .tx-borrar button").click()
    pag.wait_for_timeout(1500)
    despues = pag.locator(".tx").count()
    notas.append(f"y borra de verdad: {antes} -> {despues} movimientos")
    notas.append("el que se ha ido es el que tocaba: " + str(texto not in pag.content()))

    # tirar para actualizar
    pag.evaluate("""() => {
        const main = document.querySelector('#main');
        main.scrollTop = 0;
        const toque = (tipo, cy) => document.dispatchEvent(new TouchEvent(tipo, {
            bubbles: true, cancelable: true,
            touches: tipo === 'touchend' ? [] : [new Touch({identifier: 2, target: main, clientX: 190, clientY: cy})],
            changedTouches: [new Touch({identifier: 2, target: main, clientX: 190, clientY: cy})]}));
        toque('touchstart', 120); toque('touchmove', 150); toque('touchmove', 210);
        window.__aviso = document.querySelector('#tirar').className;
        toque('touchend', 210);
    }""")
    pag.wait_for_timeout(300)
    notas.append("al tirar sale el aviso: " + str("visible" in pag.evaluate("window.__aviso")))
    pag.wait_for_timeout(2000)
    notas.append("y luego desaparece: " + str(not pag.locator("#tirar.visible").count()))
    notas.append("la lista sigue en su sitio: " + str(pag.locator(".tx").count() == despues))
    nav.close()
open("/out/gestos.txt", "w").write("\n".join(notas))
