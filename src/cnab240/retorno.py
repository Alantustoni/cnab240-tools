"""
retorno.py — Combinador de arquivos de RETORNO (pagamento / SISPAG).

Diferente do Juntador RET (que trata cobrança — segmentos G/H/Y), estes
são arquivos de retorno de PAGAMENTO do Itaú (segmentos A/B/J) com
estrutura de VÁRIOS LOTES por arquivo:

    header arquivo (tipo 0)
      header lote (tipo 1) · detalhes (tipo 3: A/B/J) · trailer lote (tipo 5)
      ... (vários lotes)
    trailer arquivo (tipo 9)

Cada arquivo é de UM dia. O SISPAG aceita vários dias num arquivo só, desde
que a estrutura fique íntegra. A junção correta:

  • mantém 1 header e 1 trailer de arquivo;
  • empilha TODOS os lotes e os RENUMERA em sequência (0001, 0002, …) —
    cada arquivo começa no lote 0001, então sem renumerar daria repetido;
  • RECALCULA o trailer do arquivo (qtd de lotes e de registros);
  • separa por empresa: se misturar empresas, mantém a MAIORIA (pelo header)
    e retira o(s) intruso(s); empate não remove nada, só avisa;
  • ordena os arquivos por data do header (cronológico).

Nada dos detalhes é alterado — só o número do lote [3:7] e o trailer final.
"""
from __future__ import annotations

import logging
import os
import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# posições (0-based) do leiaute CNAB240 usadas aqui
_TIPO       = slice(7, 8)     # 0=header arq · 1=header lote · 3=detalhe · 5=trailer lote · 9=trailer arq
_LOTE       = slice(3, 7)     # número do lote
_SEGMENTO   = slice(13, 14)   # A/B/J no detalhe
_TRL_LOTES  = slice(17, 23)   # trailer arquivo: qtd de lotes
_TRL_REG    = slice(23, 29)   # trailer arquivo: qtd de registros
_HDR_DATA   = slice(143, 151) # header arquivo: data de geração (DDMMAAAA)


def _sem_acento(texto: str) -> str:
    t = unicodedata.normalize("NFKD", (texto or "").upper())
    return t.encode("ascii", "ignore").decode("ascii")


@dataclass
class ResultadoRetorno:
    linhas_finais:   List[str] = field(default_factory=list)
    log:             List[str] = field(default_factory=list)
    avisos:          List[str] = field(default_factory=list)
    arquivos_recebidos: int = 0
    total_arquivos:  int = 0          # arquivos efetivamente combinados
    total_lotes:     int = 0
    total_registros: int = 0
    total_detalhes:  int = 0
    segmentos:       Dict[str, int] = field(default_factory=dict)
    empresa_predominante: Optional[str] = None
    arquivos_fora_empresa: List[str] = field(default_factory=list)  # intrusos
    arquivos_invalidos: List[str] = field(default_factory=list)     # estrutura ruim

    @property
    def ok(self) -> bool:
        return self.total_registros > 0 and bool(self.linhas_finais)


def _tipo(linha: str) -> str:
    return linha[_TIPO] if len(linha) >= 8 else ""


def _empresa_do_arquivo(linhas: List[str], empresas: List[str]) -> Optional[str]:
    """Empresa dona da conta, pelo header arquivo (tipo 0)."""
    termos = [(e, _sem_acento(e)) for e in empresas]
    for l in linhas:
        if _tipo(l) == "0":
            alvo = _sem_acento(l)
            for original, termo in termos:
                if termo and termo in alvo:
                    return original.upper()
            return None
    return None


def _data_do_arquivo(linhas: List[str]) -> Optional[datetime]:
    """Data de geração do header arquivo (DDMMAAAA em [143:151])."""
    for l in linhas:
        if _tipo(l) == "0" and len(l) >= 151:
            txt = l[_HDR_DATA].strip()
            if txt.isdigit() and len(txt) == 8 and txt != "00000000":
                try:
                    return datetime.strptime(txt, "%d%m%Y")
                except ValueError:
                    return None
    return None


def _validar_arquivo(linhas: List[str]) -> Optional[str]:
    """
    Valida a estrutura de UM arquivo de retorno. Devolve None quando está
    OK, ou a descrição do problema:
      • 1 header arq (tipo 0), 1 trailer arq (tipo 9), ≥1 lote (tipo 1);
      • cada lote com o seu trailer (nº de tipo 5 == nº de tipo 1) —
        arquivo truncado geraria um combinado que o SISPAG rejeita inteiro;
      • detalhes de PAGAMENTO (A/B/J…) — segmento G/H é arquivo de
        COBRANÇA (do Juntador RET), selecionado por engano.
    """
    h0 = sum(1 for l in linhas if _tipo(l) == "0")
    t9 = sum(1 for l in linhas if _tipo(l) == "9")
    l1 = sum(1 for l in linhas if _tipo(l) == "1")
    l5 = sum(1 for l in linhas if _tipo(l) == "5")
    if not (h0 == 1 and t9 == 1 and l1 >= 1):
        return "estrutura CNAB inesperada"
    if l5 != l1:
        return (f"lote sem trailer ({l1} header(s) de lote × {l5} "
                "trailer(s)) — arquivo truncado?")
    segs = {l[_SEGMENTO].upper() for l in linhas if _tipo(l) == "3"}
    if segs & {"G", "H", "Y"}:
        return ("contém segmentos G/H de COBRANÇA — parece arquivo do "
                "Juntador RET, não retorno de pagamento")
    return None


def _banco_do_arquivo(linhas: List[str]) -> str:
    """Código do banco (3 primeiros dígitos do header arquivo)."""
    for l in linhas:
        if _tipo(l) == "0":
            return l[0:3]
    return "?"


def _separar_por_empresa(
    lidos: List[Tuple[Path, List[str]]],
    deteccao: Dict[Path, Optional[str]],
    r: ResultadoRetorno,
) -> List[Tuple[Path, List[str]]]:
    """Mantém a empresa da MAIORIA; empate não remove nada (só avisa)."""
    contagem = Counter(e for e in deteccao.values() if e)
    if len(contagem) < 2:
        if contagem:
            r.empresa_predominante = contagem.most_common(1)[0][0]
        return lidos

    ordenado = contagem.most_common()
    if ordenado[0][1] == ordenado[1][1]:
        resumo = ", ".join(f"{e}: {n}" for e, n in ordenado)
        r.avisos.append(
            f"Empate na detecção de empresa ({resumo}). Nenhum arquivo foi "
            "removido — confira se não misturou empresas.")
        return lidos

    predominante = ordenado[0][0]
    r.empresa_predominante = predominante
    mantidos, removidos = [], []
    for arq, linhas in lidos:
        emp = deteccao.get(arq)
        if emp is None or emp == predominante:
            mantidos.append((arq, linhas))
        else:
            removidos.append(f"{Path(arq).name} ({emp})")
    if removidos:
        r.arquivos_fora_empresa = removidos
        r.avisos.append(
            f"Empresa predominante: {predominante}. {len(removidos)} arquivo(s) "
            "de outra empresa REMOVIDOS: " + "; ".join(removidos))
        r.log.append(f"• Separação por empresa: mantida {predominante}; "
                     f"removidos {len(removidos)} intruso(s).")
    return mantidos


def combinar(arquivos: List[Path], cfg_retorno: Dict[str, Any]) -> ResultadoRetorno:
    """
    Combina vários arquivos de retorno diários num arquivo único e íntegro.
    Não grava nada — quem chama grava (separação testável).
    """
    separar = bool(cfg_retorno.get("separar_por_empresa", True))
    ordenar = bool(cfg_retorno.get("ordenar_por_data", True))
    empresas = list(cfg_retorno.get("empresas", ["ALFAMETAL", "BETAVIARIA"]))

    r = ResultadoRetorno()
    r.arquivos_recebidos = len(arquivos)

    # ── lê e valida a estrutura de cada arquivo ─────────────────────────
    lidos: List[Tuple[Path, List[str]]] = []
    for arq in arquivos:
        try:
            texto = Path(arq).read_text(encoding="latin-1", errors="ignore")
        except OSError as exc:
            r.arquivos_invalidos.append(f"{Path(arq).name} (não consegui ler: {exc})")
            continue
        linhas = [l.rstrip("\r\n") for l in texto.splitlines() if l.strip()]
        problema = _validar_arquivo(linhas)
        if problema:
            r.arquivos_invalidos.append(f"{Path(arq).name} ({problema})")
            r.log.append(f"✖ {Path(arq).name} — {problema} (ignorado)")
            continue
        lidos.append((arq, linhas))

    # ── todos do MESMO banco (não misturar 341 com 237 em silêncio) ─────
    if lidos:
        bancos = Counter(_banco_do_arquivo(linhas) for _, linhas in lidos)
        if len(bancos) > 1:
            predominante_banco = bancos.most_common(1)[0][0]
            restantes = []
            for arq, linhas in lidos:
                b = _banco_do_arquivo(linhas)
                if b == predominante_banco:
                    restantes.append((arq, linhas))
                else:
                    r.arquivos_invalidos.append(
                        f"{Path(arq).name} (banco {b} ≠ {predominante_banco})")
                    r.log.append(f"✖ {Path(arq).name} — banco {b} diferente "
                                 f"da maioria ({predominante_banco}) — ignorado")
            lidos = restantes

    if r.arquivos_invalidos:
        r.avisos.append(
            f"{len(r.arquivos_invalidos)} arquivo(s) IGNORADOS: "
            + "; ".join(r.arquivos_invalidos))
    if not lidos:
        r.avisos.append("Nenhum arquivo válido para combinar.")
        return r
    if len(lidos) > 0:
        total_lotes_previsto = sum(
            1 for _, linhas in lidos for l in linhas if _tipo(l) == "1")
        if total_lotes_previsto > 9999:
            r.avisos.append(
                f"{total_lotes_previsto} lotes no total — o campo de lote do "
                "CNAB240 só vai até 9999. Combine menos arquivos por vez.")
            return r

    # ── detecta empresa e separa intrusos (maioria) ─────────────────────
    deteccao = {arq: _empresa_do_arquivo(linhas, empresas) for arq, linhas in lidos}
    if separar and len(lidos) > 1:
        lidos = _separar_por_empresa(lidos, deteccao, r)
    elif deteccao:
        cont = Counter(e for e in deteccao.values() if e)
        if cont:
            r.empresa_predominante = cont.most_common(1)[0][0]

    # ── ordena por data do header (cronológico), estável ────────────────
    if ordenar:
        lidos = sorted(
            enumerate(lidos),
            key=lambda par: (_data_do_arquivo(par[1][1]) or datetime.max, par[0]))
        lidos = [t for _, t in lidos]

    # ── combina: 1 header, todos os lotes renumerados, 1 trailer ────────
    header_arq: Optional[str] = None
    trailer_arq: Optional[str] = None
    lotes: List[List[str]] = []
    extraviadas = 0
    for arq, linhas in lidos:
        atual: Optional[List[str]] = None
        for l in linhas:
            tp = _tipo(l)
            if tp == "0":
                if header_arq is None:
                    header_arq = l
            elif tp == "9":
                trailer_arq = l
                atual = None                    # nada depois do tipo 9 entra
            elif tp == "1":
                atual = [l]
                lotes.append(atual)
            elif tp == "5" and atual is not None:
                atual.append(l)
                atual = None                    # trailer FECHA o lote
            elif atual is not None:
                atual.append(l)
            else:
                extraviadas += 1                # linha fora de qualquer lote
        r.log.append(f"✔ {Path(arq).name} — "
                     f"{sum(1 for l in linhas if _tipo(l) == '1')} lote(s)")
    if extraviadas:
        r.avisos.append(
            f"{extraviadas} linha(s) fora de qualquer lote foram DESCARTADAS "
            "(arquivo de origem fora do padrão). Confira os arquivos.")

    final: List[str] = [header_arq]
    for i, lote in enumerate(lotes, start=1):
        num = f"{i:04d}"
        for l in lote:
            final.append(l[:3] + num + l[7:])   # reescreve só o número do lote

    qtd_lotes = len(lotes)
    qtd_reg = len(final) + 1                     # +1 = o próprio trailer arquivo
    trailer_arq = (trailer_arq or "").ljust(240)  # nunca fatiar linha curta
    final.append(trailer_arq[:17] + f"{qtd_lotes:06d}"
                 + f"{qtd_reg:06d}" + trailer_arq[29:])

    r.linhas_finais = final
    r.total_arquivos = len(lidos)
    r.total_lotes = qtd_lotes
    r.total_registros = qtd_reg
    seg = Counter(l[_SEGMENTO] for l in final if _tipo(l) == "3")
    r.segmentos = dict(seg)
    r.total_detalhes = sum(seg.values())
    r.log.append(
        f"• Combinado: {len(lidos)} arquivo(s) → {qtd_lotes} lotes "
        f"(renumerados 0001..{qtd_lotes:04d}) · {qtd_reg} registros · "
        f"trailer recalculado.")
    return r


def gravar(resultado: ResultadoRetorno, arquivo_saida: Path) -> None:
    """Grava o arquivo combinado — CRLF, latin-1, escrita atômica."""
    saida = Path(arquivo_saida)
    fd, tmp = tempfile.mkstemp(dir=saida.parent, suffix=".tmp.ret")
    try:
        with os.fdopen(fd, "w", encoding="latin-1", newline="") as f:
            f.write("\r\n".join(resultado.linhas_finais) + "\r\n")
        os.replace(tmp, saida)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    logger.info("Retorno: gravado %s (%d linhas)", saida.name,
                len(resultado.linhas_finais))
