"""Comprueba pinchando de verdad: archivar, desarchivar, ordenar y borrar."""
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
notas = []
with sync_playwright() as p:
    nav = p.webkit.launch()
    pag = nav.new_context(viewport={"width": 390, "height": 844}, is_mobile=True,
                          has_touch=True, locale="es-ES").new_page()
    pag.on("pageerror", lambda e: notas.append("error JS: " + str(e)))
    pag.on("dialog", lambda d: d.accept())

    def fila(nombre):
        return pag.locator("#sobres .row-item", has_text=nombre).first

    # crear un sobre de prueba
    pag.goto(BASE + "/ajustes?abierto=sobres", wait_until="networkidle")
    pag.locator("#sobres .row-item.nuevo summary").click()
    pag.fill('#sobres .row-item.nuevo input[name="nombre"]', "Sobre de prueba")
    pag.locator('#sobres .row-item.nuevo button[type="submit"]').click()
    pag.wait_for_timeout(1200)
    notas.append("creado: " + str(fila("Sobre de prueba").count() > 0))

    # archivar
    fila("Sobre de prueba").locator("summary").click()
    pag.wait_for_timeout(300)
    fila("Sobre de prueba").get_by_role("button", name="Archivar").click()
    pag.wait_for_timeout(1200)
    notas.append("archivado: " + str("archivado" in fila("Sobre de prueba").inner_text()))
    notas.append("ya no sale al apuntar: " +
                 str(pag.locator('#txform-qa .chip', has_text="Sobre de prueba").count() == 0))

    # reactivar
    fila("Sobre de prueba").locator("summary").click()
    pag.wait_for_timeout(300)
    fila("Sobre de prueba").get_by_role("button", name="Reactivar").click()
    pag.wait_for_timeout(1200)
    notas.append("reactivado: " + str("archivado" not in fila("Sobre de prueba").inner_text()))

    # ordenar
    antes = pag.locator("#sobres .row-item summary span:first-child").all_inner_texts()
    fila("Sobre de prueba").locator("summary").click()
    pag.wait_for_timeout(300)
    fila("Sobre de prueba").get_by_role("button", name="Subir").click()
    pag.wait_for_timeout(1200)
    despues = pag.locator("#sobres .row-item summary span:first-child").all_inner_texts()
    notas.append(f"ordenar: {antes[-2]} -> {despues[-2]} (sube una posición: " +
                 str(antes[-2] != despues[-2]) + ")")

    # borrar (no tiene movimientos)
    fila("Sobre de prueba").locator("summary").click()
    pag.wait_for_timeout(300)
    fila("Sobre de prueba").get_by_role("button", name="Borrar").click()
    pag.wait_for_timeout(1400)
    notas.append("borrado: " + str(pag.locator("#sobres .row-item", has_text="Sobre de prueba").count() == 0))

    # uno con movimientos no ofrece borrar
    con_historial = pag.locator("#sobres .row-item", has_text="Hucha coche").first
    con_historial.locator("summary").click()
    pag.wait_for_timeout(300)
    notas.append("«Hucha coche» (con movimientos) no tiene botón de borrar: " +
                 str(con_historial.get_by_role("button", name="Borrar").count() == 0))
    pag.screenshot(path="/out/ajustes-sobres.png", full_page=True)
    nav.close()
open("/out/prueba_ajustes.txt", "w").write("\n".join(notas))
