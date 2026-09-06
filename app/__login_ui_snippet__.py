@app.get("/login")
def login_ui():
    """Terminal-style page: enter IG credentials, watch log, get session JSON."""
    return HTMLResponse(login_page.LOGIN_HTML,
                        headers={"Cache-Control": "no-store, max-age=0"})
