"""
Cliente de autenticación contra el SII (Servicio de Impuestos Internos, Chile).

Módulo 1 del ERP: iniciar sesión con RUT + Clave Tributaria y mantener una
sesión HTTP autenticada. Esa sesión es la base para el Módulo 2 (descarga del
Registro de Compras y Ventas, RCV).

Notas de diseño:
- No se almacena la clave. La sesión vive en memoria mientras dure la sesión web.
- Los endpoints del SII pueden cambiar; se dejan como constantes arriba para
  poder ajustarlos fácilmente durante las pruebas con credenciales reales.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import requests

# --- Endpoints del SII (ajustables) ---------------------------------------
SII_AUTH_URL = "https://zeusr.sii.cl/cgi_AUT2000/CAutInicio.cgi"
SII_HOME_URL = "https://misii.sii.cl/cgi_misii/siihome.cgi"
SII_SELEMP_URL = "https://www1.sii.cl/cgi-bin/Portal001/mipeSelEmpresa.cgi"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class SIIAuthError(Exception):
    """Error de autenticación o de comunicación con el SII."""


class SIISessionExpirada(Exception):
    """La sesión ya autenticada con el SII se perdió a mitad de uso (p. ej.
    por inactividad prolongada).

    Se distingue de SIIAuthError porque no es un problema de credenciales: el
    login original fue correcto, pero el SII cerró la sesión de su lado. No
    se reintenta sola ni se guarda la clave para reloguear en silencio; quien
    la capture debe invalidar la sesión guardada y pedirle al usuario que
    vuelva a ingresar su Clave Tributaria."""


def normalizar_rut(rut_raw: str) -> tuple[str, str]:
    """Devuelve (numero_sin_dv, dv) a partir de cualquier formato de RUT.

    Acepta '76.123.456-7', '76123456-7', '761234567', '12.345.678-K', etc.
    """
    limpio = re.sub(r"[^0-9kK]", "", rut_raw or "").upper()
    if len(limpio) < 2:
        raise SIIAuthError("El RUT ingresado no es válido.")
    numero, dv = limpio[:-1], limpio[-1]
    if not numero.isdigit():
        raise SIIAuthError("El RUT ingresado no es válido.")
    return numero, dv


def dv_valido(numero: str, dv: str) -> bool:
    """Valida el dígito verificador (módulo 11) de un RUT chileno."""
    suma, factor = 0, 2
    for d in reversed(numero):
        suma += int(d) * factor
        factor = 2 if factor == 7 else factor + 1
    resto = 11 - (suma % 11)
    esperado = {11: "0", 10: "K"}.get(resto, str(resto))
    return esperado == dv.upper()


@dataclass
class SIIClient:
    """Mantiene una sesión HTTP autenticada con el SII."""

    session: requests.Session = field(default_factory=requests.Session)
    rut: str | None = None
    rut_empresa: str | None = None

    def __post_init__(self) -> None:
        self.session.headers.update({"User-Agent": _UA})

    def seleccionar_empresa(self, rut_emp: str, desde_donde: str = "OPCION=1&TIPO=4") -> None:
        """Selecciona la empresa activa en el sistema de facturación gratuito.

        Equivale a elegir la empresa en el desplegable de mipeSelEmpresa.cgi.
        Deja la sesión apuntando a esa empresa para las consultas siguientes.
        """
        try:
            self.session.post(
                SII_SELEMP_URL,
                data={"RUT_EMP": rut_emp, "DESDE_DONDE_URL": desde_donde},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise SIIAuthError(f"No se pudo seleccionar la empresa {rut_emp}: {exc}") from exc
        self.rut_empresa = rut_emp

    def login(self, rut_raw: str, clave: str) -> None:
        """Autentica contra el SII. Lanza SIIAuthError si falla."""
        numero, dv = normalizar_rut(rut_raw)
        if not dv_valido(numero, dv):
            raise SIIAuthError("El dígito verificador del RUT no es correcto.")

        payload = {
            "rut": numero,
            "dv": dv,
            "referencia": SII_HOME_URL,
            "411": "",
            "rutcntr": f"{numero}-{dv}",
            "clave": clave,
        }
        try:
            self.session.post(SII_AUTH_URL, data=payload, timeout=30)
            home = self.session.get(SII_HOME_URL, timeout=30)
        except requests.RequestException as exc:
            raise SIIAuthError(f"No se pudo conectar con el SII: {exc}") from exc

        texto = home.text.lower()

        # Señales de credenciales inválidas
        if any(s in texto for s in ("clave incorrecta", "no coinciden", "usuario y/o clave", "clave errada")):
            raise SIIAuthError("RUT o Clave Tributaria incorrectos.")

        # Señales de sesión iniciada correctamente
        autenticado = (
            bool(self.session.cookies.get("TOKEN"))
            or "cerrar sesión" in texto
            or "cerrar sesion" in texto
            or "mi sii" in texto
        )
        if not autenticado:
            raise SIIAuthError(
                "No se pudo verificar la sesión con el SII. "
                "Revisa el RUT y la clave, o inténtalo nuevamente."
            )

        self.rut = f"{numero}-{dv}"

    def logout(self) -> None:
        self.session.close()
        self.rut = None
