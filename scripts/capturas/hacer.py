"""Capturas con WebKit (motor de Safari) a tamano de iPhone."""
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
PAGINAS = [("panel", "/"), ("movimientos", "/movimientos"), ("sobres", "/sobres"),
           ("presupuesto", "/presupuesto"), ("resumen", "/resumen"), ("deudas", "/deudas"),
           ("ajustes", "/ajustes"), ("mas", "/mas"), ("nuevo", "/nuevo"), ("sobre", "/sobres/2"), ("ajustes-sobras", "/ajustes?abierto=sobras")]

with sync_playwright() as p:
    nav = p.webkit.launch()
    ctx = nav.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                          is_mobile=True, has_touch=True, locale="es-ES",
                          color_scheme="light")
    pag = ctx.new_page()
    errores = []
    pag.on("console", lambda m: errores.append(m.text) if m.type == "error" else None)
    pag.on("pageerror", lambda e: errores.append(str(e)))
    for nombre, ruta in PAGINAS:
        pag.goto(BASE + ruta, wait_until="networkidle")
        pag.screenshot(path=f"/out/{nombre}.png", full_page=True)
        ancho = pag.evaluate("document.documentElement.scrollWidth")
        if ancho > 390:
            errores.append(f"{nombre}: scroll horizontal ({ancho}px)")
    # la hoja de apuntar, abierta
    pag.goto(BASE + "/", wait_until="networkidle")
    pag.click(".fab")
    pag.wait_for_timeout(400)
    pag.screenshot(path="/out/hoja-apuntar.png")
    # con el tipo "Aporte a sobre" elegido
    pag.click('label.type.type-aporte_sobre span')
    pag.wait_for_timeout(250)
    pag.screenshot(path="/out/hoja-aporte.png")
    # modo oscuro
    ctx2 = nav.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                           is_mobile=True, has_touch=True, locale="es-ES", color_scheme="dark")
    p2 = ctx2.new_page()
    for nombre, ruta in [("panel-oscuro", "/"), ("resumen-oscuro", "/resumen")]:
        p2.goto(BASE + ruta, wait_until="networkidle")
        p2.screenshot(path=f"/out/{nombre}.png", full_page=True)
    open("/out/errores.txt", "w").write("\n".join(errores) or "sin errores de consola")
    nav.close()

# ---- prueba de interacción: apuntar un gasto desde la hoja del botón +
with sync_playwright() as p:
    nav = p.webkit.launch()
    ctx = nav.new_context(viewport={"width": 390, "height": 844}, is_mobile=True,
                          has_touch=True, locale="es-ES")
    pag = ctx.new_page()
    fallos = []
    pag.on("pageerror", lambda e: fallos.append(str(e)))
    pag.on("dialog", lambda d: d.accept())      # los «¿seguro?» de borrar
    pag.goto(BASE + "/", wait_until="networkidle")
    pag.click(".fab")
    pag.fill('#txform-qa input[name="importe"]', "12,34")
    pag.click('#txform-qa label.chip:has-text("Ocio")')
    pag.click('button[form="txform-qa"]')            # el "Guardar" de la cabecera
    pag.wait_for_timeout(1200)
    if pag.is_visible("dialog#qa[open]"):
        fallos.append("la hoja no se ha cerrado al guardar")
    if not pag.is_visible("#toast.on"):
        fallos.append("no ha salido el aviso")
    else:
        pag.screenshot(path="/out/aviso-guardado.png")
    pag.goto(BASE + "/movimientos", wait_until="networkidle")
    if pag.locator('.tx:has-text("12,34")').count() != 1:
        fallos.append("el movimiento no aparece una sola vez en la lista")
    # editar y borrar
    pag.click('.tx:has-text("12,34")')
    pag.wait_for_timeout(800)
    pag.fill('#txform input[name="concepto"]', "Café con Ana")
    pag.click('#txform button[type="submit"]')
    pag.wait_for_timeout(900)
    if pag.locator('.tx:has-text("Café con Ana")').count() != 1:
        fallos.append("la edición no se ha guardado")
    pag.click('.tx:has-text("12,34")')
    pag.wait_for_timeout(800)
    pag.click('form[action$="/borrar"] button')
    pag.wait_for_timeout(1500)
    # ojo: el aviso «Movimiento borrado (gasto de 12,34 €)» también lleva el importe,
    # así que se mira la lista, no la página entera
    if pag.locator('.tx:has-text("12,34")').count():
        fallos.append("el movimiento no se ha borrado")
    # cuadre
    pag.goto(BASE + "/sobres", wait_until="networkidle")
    pag.fill("#saldo-tr", "3.400,00")
    pag.click('.cuadre-form button[type="submit"]')
    pag.wait_for_timeout(900)
    if "sin apuntar" not in pag.content():
        fallos.append("el cuadre no ha respondido")
    open("/out/interaccion.txt", "w").write("\n".join(fallos) or "interacción: todo correcto")
    nav.close()
