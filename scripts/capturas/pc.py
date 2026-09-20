"""Capturas de la versión de ordenador (y comprobación de la barra del móvil)."""
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
PAGINAS = [("pc-panel", "/"), ("pc-movimientos", "/movimientos"), ("pc-sobres", "/sobres"),
           ("pc-presupuesto", "/presupuesto"), ("pc-resumen", "/resumen"), ("pc-ajustes", "/ajustes")]
notas = []
with sync_playwright() as p:
    nav = p.webkit.launch()
    ctx = nav.new_context(viewport={"width": 1280, "height": 820}, locale="es-ES",
                          color_scheme="dark")
    pag = ctx.new_page()
    pag.on("pageerror", lambda e: notas.append("error JS: " + str(e)))
    for nombre, ruta in PAGINAS:
        pag.goto(BASE + ruta, wait_until="networkidle")
        pag.screenshot(path=f"/out/{nombre}.png", full_page=(nombre == "pc-panel"))
        if pag.evaluate("document.documentElement.scrollWidth") > 1280:
            notas.append(f"{ruta}: se sale a lo ancho")
        hueco = pag.evaluate("""() => {
            const h = document.querySelector('.top').getBoundingClientRect();
            const p = document.querySelector('main').firstElementChild.getBoundingClientRect();
            return Math.round(p.top - h.bottom); }""")
        if hueco > 40:
            notas.append(f"{ruta}: hueco de {hueco}px entre la cabecera y el contenido")

    # pantalla ancha
    ancha = nav.new_context(viewport={"width": 1920, "height": 1000}, locale="es-ES", color_scheme="dark").new_page()
    for nombre, ruta in [("ancha-panel", "/"), ("ancha-movimientos", "/movimientos"), ("ancha-ajustes", "/ajustes")]:
        ancha.goto(BASE + ruta, wait_until="networkidle")
        ancha.screenshot(path=f"/out/{nombre}.png")
    caja = ancha.evaluate("""() => { const m = document.querySelector('main').getBoundingClientRect();
        return {izq: Math.round(m.left), der: Math.round(innerWidth - m.right), ancho: Math.round(m.width)}; }""")
    notas.append(f"a 1920px -> contenido de {caja['ancho']}px, margen izq {caja['izq']} / der {caja['der']}")
    notas.append("menú lateral visible: " + str(pag.is_visible(".side-add")))
    notas.append("botón flotante oculto en PC: " + str(not pag.is_visible(".fab")))
    notas.append("enlaces del menú: " + " · ".join(pag.locator(".tabs a").all_inner_texts()))
    pag.goto(BASE + "/movimientos", wait_until="networkidle")
    boton = pag.locator(".side-add")
    estilo = pag.evaluate("""() => { const b = document.querySelector('.side-add'); const s = getComputedStyle(b);
        return s.color + ' sobre ' + s.backgroundColor; }""")
    notas.append("botón apuntar: texto " + estilo)
    pag.screenshot(path="/out/pc-menu-oscuro.png")
    boton.click()
    pag.wait_for_timeout(500)
    pag.screenshot(path="/out/pc-apuntar.png")

    # móvil: que la barra de abajo no se mueva al hacer scroll
    m = nav.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, locale="es-ES").new_page()
    m.goto(BASE + "/", wait_until="networkidle")
    antes = m.locator(".tabs").bounding_box()
    m.evaluate("(document.querySelector('main').scrollTop = 900)")   # el móvil de WebKit no tiene rueda
    m.wait_for_timeout(600)
    despues = m.locator(".tabs").bounding_box()
    notas.append(f"barra de abajo: y={antes['y']:.0f} antes / y={despues['y']:.0f} después de bajar "
                 f"({'quieta' if abs(antes['y'] - despues['y']) < 1 else 'SE MUEVE'})")
    notas.append("la página entera scrollea: " +
                 str(m.evaluate("document.scrollingElement.scrollHeight > innerHeight + 4")))
    notas.append("el contenido scrollea por dentro: " +
                 str(m.evaluate("document.querySelector('main').scrollTop > 100")))
    m.screenshot(path="/out/movil-scroll.png")
    nav.close()
open("/out/pc.txt", "w").write("\n".join(notas))
