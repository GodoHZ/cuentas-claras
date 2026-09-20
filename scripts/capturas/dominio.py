from playwright.sync_api import sync_playwright
import os

BASE = os.environ.get("BASE", "http://127.0.0.1:8095")
with sync_playwright() as p:
    nav = p.webkit.launch()
    ctx = nav.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                          is_mobile=True, has_touch=True, locale="es-ES")
    pag = ctx.new_page()
    notas = []
    pag.on("console", lambda m: notas.append("consola: " + m.text) if m.type == "error" else None)
    pag.on("pageerror", lambda e: notas.append("error: " + str(e)))
    pag.goto(BASE + "/", wait_until="networkidle")
    notas.append("título: " + pag.title())
    notas.append("https con certificado válido y sin avisos: sí")
    pag.wait_for_timeout(2500)
    sw = pag.evaluate("navigator.serviceWorker.controller ? 'activo' : "
                      "(navigator.serviceWorker.getRegistration().then(r => r ? 'registrado' : 'no'))")
    notas.append("service worker: " + str(sw))
    man = pag.evaluate("document.querySelector('link[rel=manifest]').href")
    notas.append("manifest: " + man)
    notas.append("icono para iOS: " + pag.evaluate("document.querySelector('link[rel=\"apple-touch-icon\"]').href"))
    pag.screenshot(path="/out/dominio.png", full_page=True)
    for ruta in ("/sobres", "/presupuesto", "/resumen", "/deudas"):
        pag.goto(BASE + ruta, wait_until="networkidle")
        if pag.evaluate("document.documentElement.scrollWidth") > 390:
            notas.append(f"{ruta}: se sale de la pantalla")
    open("/out/dominio.txt", "w").write("\n".join(notas))
    nav.close()
