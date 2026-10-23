"""
importacao_usinas.py — Importação Usinas.

Monta um arquivo de importação de códigos de barras a partir de uma planilha
de "Boletos Recebidos" (DDA) — .csv OU .xlsx — no MESMO leiaute do Juntador
RET (segmento G + complemento, trailers recalculados).

Fluxo:
  1. Lê a planilha (Pagador; Beneficiário; CNPJ; Venc.; Nº Doc.; A Pagar; …;
     Código de barras). Aceita CSV e XLSX.
  2. Usa um MOLDE de registro (embutido, padrão do sistema — ou de um RET
     que o usuário aponte) e troca só os campos que vêm da planilha:
        [17:61]  código de barras (44 díg)
        [63:77]  CNPJ do favorecido (14 díg) — depois duplicado por filial
        [77:107] nome do favorecido
        [107:115] vencimento (DDMMAAAA)
        [115:130] valor (centavos, 15 díg)
        [147:162] número da nota (Nº Doc., inteiro)
  3. Duplica por filial (CNPJ matriz → filiais do cadastro), ordena por
     vencimento, renumera o sequencial e recalcula os trailers.

Como o leiaute é sempre o mesmo, o MOLDE vem embutido — basta anexar a
planilha. Se um dia o leiaute mudar, dá para apontar um RET de molde.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import ret as _ret

logger = logging.getLogger(__name__)

# Fator de vencimento do código de barras: fator 1000 = 22/02/2025
# (reset Febraban de 2025). Usado para gravar no campo de vencimento a data
# QUE ESTÁ NA BARRA — que é a que o título usa — em vez da data da planilha
# (que pode estar prorrogada e divergir da barra, derrubando o vínculo).
_FATOR_BASE = date(2025, 2, 22)

# Posições no segmento G (leiaute medido nos RET reais do Alan).
_BARRA_INI, _BARRA_FIM = 17, 61      # código de barras (44 díg)
_CNPJ14_INI            = 63          # 14 díg do CNPJ (dentro de [62:77])
_NOME_INI              = 77          # [77:107]
_VENC_INI              = 107         # [107:115]
_VALOR_INI             = 115         # [115:130]
_DOC_INI               = 147         # [147:162]

# ── MOLDE PADRÃO (embutido) ────────────────────────────────────────────
# Um registro real (Usiminas) já convertido para Bradesco, usado como base
# do leiaute. Só os 6 campos acima são reescritos por boleto; o resto é
# herdado deste molde (leiaute que o banco já aceitava). Se o leiaute mudar,
# passe um RET em `ret_molde_path` que ele extrai um molde novo de lá.
# Linha única EXATA (registro real Usiminas, 341→237). Não quebrar em pedaços:
# as posições dos campos dependem da contagem exata de caracteres.
DEFAULT_MOLDE_G = "2370001300007G 01237931586000707601800000000000000000000000032090100200000109USINAS SIDERURGICAS DE MINAS G01102026000000007076018000000000000000012345678901     000000          10216092026000000000012029 00000000000000000000000000301220261".ljust(240)[:240]

DEFAULT_MOLDE_COMPL = "2370001300008Y 010000000000000000                                         00000000000000000000000 00000000000000000000000301102026000000000000000000000000000000".ljust(240)[:240]


def _validar_molde(molde_g: str) -> bool:
    """
    Confere que o molde está alinhado: é segmento G e o valor embutido no
    código de barras (posições 9-18 da barra) bate com o campo de valor.
    Se não bater, as posições dos campos estão erradas.
    """
    if len(molde_g) < 162 or molde_g[13:14] != "G":
        return False
    barra = molde_g[_BARRA_INI:_BARRA_FIM]
    if len(barra) != 44 or not barra.isdigit():
        return False
    valor_barra = barra[9:19]                       # valor dentro da barra
    valor_campo = molde_g[_VALOR_INI:_VALOR_INI + 15][-10:]  # 10 últimos díg
    return valor_barra == valor_campo


if not _validar_molde(DEFAULT_MOLDE_G):             # pragma: no cover
    logger.error("MOLDE PADRÃO desalinhado — confira DEFAULT_MOLDE_G.")


def _norm(texto: Any) -> str:
    t = unicodedata.normalize("NFKD", str(texto or "").upper())
    return t.encode("ascii", "ignore").decode("ascii").strip()


def _so_digitos(s: Any) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _valor_centavos(txt: Any) -> Optional[int]:
    t = str(txt or "").strip().replace("R$", "").replace(" ", "")
    if not t:
        return None
    if "," in t:                       # BR: '.' milhar, ',' decimal
        t = t.replace(".", "").replace(",", ".")
    try:
        return int(round(float(t) * 100))
    except ValueError:
        return None


def _venc_ddmmaaaa(txt: Any) -> Optional[str]:
    t = str(txt or "").strip()
    if not t:
        return None
    # ISO com hora (vindo de célula de data do Excel): pega só a data
    if re.match(r"^\d{4}-\d{2}-\d{2}", t):
        t = t[:10]
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(t, fmt).strftime("%d%m%Y")
        except ValueError:
            continue
    return None


def _tratar_nota(nota_bruta: Any, cfg_usinas: Optional[Dict[str, Any]]) -> str:
    """
    Transforma o 'Nº Doc.' do boleto na NF que o sistema guarda.

    O Nº Doc vem como  0 + NF + parcela  (ex.: '0146463201' = 0 + 1464632 + 01).
    O sistema guarda só a NF ('1464632'). Então:
      1. corta a PARCELA do fim (2 dígitos no boleto), com proteção de tamanho
         mínimo;
      2. tira os zeros da frente (o sistema guarda a NF como número).

    Obs.: NÃO uso as "regras especiais" do organizador de planilhas de usinas (ex.: termina em
    '001' → corta 3). Aquilo é do formato da planilha de usinas; no boleto a
    parcela é sempre de 2 dígitos, então cortar 3 tiraria um dígito da NF.
    """
    dig = re.sub(r"\D", "", str(nota_bruta or ""))
    if not dig:
        return ""
    cfg = cfg_usinas or {}
    corte = int(cfg.get("nf_digitos_sufixo_padrao", 2))
    tam_min = int(cfg.get("nf_tamanho_minimo", 3))
    if corte > 0 and len(dig) - corte >= tam_min:
        dig = dig[:-corte]
    dig = dig.lstrip("0")
    return dig or "0"


@dataclass
class BoletoCSV:
    cnpj: str
    venc: str
    nota: str
    valor_centavos: int
    nome: str
    barra: str
    linha_num: int


@dataclass
class ResultadoImportacao:
    linhas_finais:      List[str] = field(default_factory=list)
    log:                List[str] = field(default_factory=list)
    avisos:             List[str] = field(default_factory=list)
    total_lidos:        int = 0
    total_validos:      int = 0
    total_registros:    int = 0
    titulos_duplicados: int = 0
    colisoes:           List[str] = field(default_factory=list)
    total_colisoes:     int = 0
    empresa_csv:        Optional[str] = None
    primeiro_vencimento: Optional[str] = None
    ultimo_vencimento:   Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.total_registros > 0


# ------------------------------------------------------------------ #
# 1) Leitura (CSV ou XLSX) → lista de boletos                          #
# ------------------------------------------------------------------ #
def _rows_do_csv(path: Path) -> List[List[str]]:
    texto = Path(path).read_text(encoding="latin-1", errors="ignore")
    linhas = texto.splitlines()
    sep = ";" if texto.count(";") >= texto.count(",") else ","
    return [ln.split(sep) for ln in linhas]


def _rows_do_xlsx(path: Path) -> List[List[str]]:
    try:
        import openpyxl
    except ImportError as e:                      # pragma: no cover
        raise ValueError(
            "Para ler .xlsx é preciso o openpyxl (pip install openpyxl).") from e
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows: List[List[str]] = []
    for row in ws.iter_rows(values_only=True):
        celulas = []
        for c in row:
            if c is None:
                celulas.append("")
            elif isinstance(c, (datetime, date)):
                celulas.append(c.strftime("%d/%m/%Y"))
            else:
                celulas.append(str(c))
        rows.append(celulas)
    wb.close()
    return rows


def _extrair_boletos(rows: List[List[str]],
                     r: ResultadoImportacao,
                     cfg_usinas: Optional[Dict[str, Any]] = None) -> List[BoletoCSV]:
    # empresa dona da conta (linha "Nome da empresa: ...")
    for row in rows[:15]:
        junto = " ".join(str(c) for c in row)
        if "NOME DA EMPRESA" in _norm(junto):
            r.empresa_csv = junto.split(":", 1)[-1].replace(";", "").strip()
            break

    # cabeçalho: linha com 'código de barras' e 'cnpj'
    header_idx = None
    cols: List[str] = []
    for i, row in enumerate(rows):
        cel = [_norm(c) for c in row]
        if (any("CODIGO DE BARRAS" in c for c in cel)
                and any(("CNPJ" in c or "CPF" in c) for c in cel)):
            header_idx, cols = i, cel
            break
    if header_idx is None:
        raise ValueError(
            "Não encontrei o cabeçalho (colunas 'Código de barras' e "
            "'CPF/CNPJ'). Confira a planilha.")

    def achar(*chaves: str) -> Optional[int]:
        for j, c in enumerate(cols):
            if any(k in c for k in chaves):
                return j
        return None

    i_cnpj  = achar("CPF/CNPJ", "CNPJ", "CPF")
    i_venc  = achar("VENC")
    i_doc   = achar("N DOC", "NO DOC", "DOCUMENTO", "DOC", "NOTA")
    i_val   = achar("A PAGAR", "VALOR")
    i_nome  = achar("BENEFICIARIO", "FAVORECIDO")
    i_barra = achar("CODIGO DE BARRAS", "LINHA DIGITAVEL", "BARRA")

    faltando = [n for n, idx in
                (("CNPJ", i_cnpj), ("Vencimento", i_venc), ("Nº Doc", i_doc),
                 ("Valor", i_val), ("Código de barras", i_barra)) if idx is None]
    if faltando:
        raise ValueError("Colunas não encontradas: " + ", ".join(faltando))

    maxi = max(i_cnpj, i_venc, i_doc, i_val, i_barra)
    boletos: List[BoletoCSV] = []
    for i in range(header_idx + 1, len(rows)):
        campos = rows[i]
        if len(campos) <= maxi:
            continue
        barra = _so_digitos(campos[i_barra])
        if len(barra) < 44:
            continue                             # rodapé / linha vazia
        r.total_lidos += 1

        cnpj  = _so_digitos(campos[i_cnpj])
        venc  = _venc_ddmmaaaa(campos[i_venc])
        valor = _valor_centavos(campos[i_val])
        nota  = _tratar_nota(campos[i_doc], cfg_usinas)   # Nº Doc -> NF do sistema
        nome  = str(campos[i_nome]).strip() if i_nome is not None else ""

        problemas = []
        if len(cnpj) != 14:
            problemas.append("CNPJ inválido")
        if venc is None:
            problemas.append("vencimento inválido")
        if valor is None or valor <= 0:
            problemas.append("valor inválido")
        if not nota:
            problemas.append("Nº Doc vazio")
        if len(barra) != 44:
            problemas.append(f"código de barras com {len(barra)} díg (esperado 44)")
        if problemas:
            r.avisos.append(f"Linha {i + 1}: {', '.join(problemas)} — ignorada.")
            continue

        boletos.append(BoletoCSV(
            cnpj=cnpj, venc=venc, nota=nota, valor_centavos=valor,
            nome=nome, barra=barra[:44], linha_num=i + 1))

    r.total_validos = len(boletos)
    return boletos


def ler_planilha(path: Path,
                 r: Optional[ResultadoImportacao] = None,
                 cfg_usinas: Optional[Dict[str, Any]] = None) -> List[BoletoCSV]:
    """Lê .csv ou .xlsx de Boletos Recebidos e devolve os boletos válidos."""
    r = r or ResultadoImportacao()
    ext = Path(path).suffix.lower()
    rows = _rows_do_xlsx(path) if ext in (".xlsx", ".xlsm") else _rows_do_csv(path)
    return _extrair_boletos(rows, r, cfg_usinas)


# ------------------------------------------------------------------ #
# 2) Molde do registro                                                #
# ------------------------------------------------------------------ #
def extrair_molde(ret_path: Path) -> Tuple[str, Optional[str]]:
    """Pega o 1º título (G + complemento) de um RET, convertido para Bradesco."""
    texto = Path(ret_path).read_text(encoding="latin-1", errors="ignore")
    linhas = [l.rstrip("\r\n") for l in texto.splitlines()]
    if _ret.detectar_banco(linhas) == _ret.BANCO_ITAU:
        linhas = [_ret.converter_itau_para_bradesco(l) for l in linhas]
    for idx, l in enumerate(linhas):
        if len(l) >= 14 and l[7:8] == "3" and l[13:14].upper() == "G":
            g = l.ljust(240)[:240]
            compl = None
            if idx + 1 < len(linhas):
                nxt = linhas[idx + 1]
                if (len(nxt) >= 14 and nxt[7:8] == "3"
                        and nxt[13:14].upper() in ("H", "Y")):
                    compl = nxt.ljust(240)[:240]
            return g, compl
    raise ValueError("Não achei um título (segmento G) no RET de molde.")


def _venc_da_barra(barra: str) -> Optional[str]:
    """
    Vencimento (DDMMAAAA) codificado no FATOR de vencimento da barra
    (posições 5-8 dos 44 dígitos). É a data que o título usa; a data da
    planilha pode estar prorrogada e divergir da barra, derrubando o vínculo.
    Devolve None se a barra não tiver fator válido (à vista/fator < 1000).
    """
    b = re.sub(r"\D", "", str(barra or ""))
    if len(b) < 9:
        return None
    fator = int(b[5:9])
    if fator < 1000:
        return None
    return (_FATOR_BASE + timedelta(days=fator - 1000)).strftime("%d%m%Y")


def _montar_registro(molde_g: str, molde_compl: Optional[str],
                     b: BoletoCSV) -> List[str]:
    # grava o vencimento QUE ESTÁ NA BARRA (não o da planilha) — é o que casa
    # com o título; cai para o venc da planilha só se a barra não tiver fator.
    venc = _venc_da_barra(b.barra) or b.venc
    g = _ret._substituir(molde_g, _BARRA_INI, b.barra)                 # [17:61]
    g = _ret._substituir(g, _CNPJ14_INI, b.cnpj)                       # [63:77]
    g = _ret._substituir(g, _NOME_INI, b.nome.ljust(30)[:30])          # [77:107]
    g = _ret._substituir(g, _VENC_INI, venc)                           # [107:115]
    g = _ret._substituir(g, _VALOR_INI, f"{b.valor_centavos:015d}")    # [115:130]
    g = _ret._substituir(g, _DOC_INI, b.nota.ljust(15)[:15])           # [147:162]
    bloco = [g]
    if molde_compl:
        compl = molde_compl
        molde_venc = molde_g[_VENC_INI:_VENC_INI + 8]
        # ajusta o vencimento no complemento (o molde carrega o venc de outro
        # boleto); troca a ocorrência do venc do molde pelo venc deste boleto.
        if molde_venc and molde_venc != venc and molde_venc in compl:
            compl = compl.replace(molde_venc, venc, 1)
        bloco.append(compl)
    return bloco


# ------------------------------------------------------------------ #
# 3) Montagem do arquivo final                                        #
# ------------------------------------------------------------------ #
def montar(
    boletos: List[BoletoCSV],
    molde_g: str,
    molde_compl: Optional[str],
    cfg_ret: Dict[str, Any],
    filiais_por_raiz: Dict[str, List[str]],
    ordenar_venc: bool = True,
    duplicar_limite: int = 0,
    duplicar_filial: bool = True,
    r: Optional[ResultadoImportacao] = None,
) -> ResultadoImportacao:
    r = r or ResultadoImportacao()

    # ── Detecta VALORES REPETIDOS (colisões) ─────────────────────────
    # O sistema casa a barra por VALOR+vencimento (não lê número nem CNPJ).
    # Então notas com o mesmo valor+venc ele NÃO distingue → uma some ou a
    # barra cai na nota errada. Aqui listamos para tratamento manual.
    from collections import defaultdict
    grupos: Dict[tuple, List[BoletoCSV]] = defaultdict(list)
    for b in boletos:
        grupos[(b.valor_centavos, b.venc)].append(b)
    for (val, venc), lst in grupos.items():
        if len(lst) > 1:
            notas = ", ".join(b.nota for b in lst)
            r.colisoes.append(
                f"R$ {val / 100:,.2f} venc {venc[:2]}/{venc[2:4]}/{venc[4:]}: "
                f"{len(lst)} notas ({notas}) — o sistema NÃO distingue; tratar à mão.")
    r.total_colisoes = sum(len(lst) for lst in grupos.values() if len(lst) > 1)
    if r.total_colisoes:
        r.avisos.append(
            f"{r.total_colisoes} nota(s) com VALOR repetido no lote — o sistema "
            "casa por valor e vai confundir/errar essas. Veja a lista de colisões.")

    registros: List[str] = []
    for b in boletos:
        registros.extend(_montar_registro(molde_g, molde_compl, b))

    # Troca de CNPJ por filial ATIVA por padrão (obrigatória): mantém a matriz
    # e acrescenta uma cópia por filial do cadastro, pois o título costuma
    # estar lançado numa filial (ex.: ARCELOR/ARMAR). Fornecedores cuja raiz
    # não está no cadastro passam intactos.
    n_dup = 0
    if duplicar_filial:
        registros, n_dup, _n = _ret._duplicar_usinas_por_filial(
            registros, filiais_por_raiz, duplicar_limite)
    r.titulos_duplicados = n_dup

    if ordenar_venc and registros:
        registros, r.primeiro_vencimento, r.ultimo_vencimento = (
            _ret._ordenar_por_vencimento(registros))

    registros = _ret._renumerar_sequencial(registros)
    r.total_registros = len(registros)
    if not registros:
        r.avisos.append("Nenhum registro válido — arquivo NÃO deve ser gerado.")
        return r

    t_lote_modelo = cfg_ret.get(
        "trailer_lote_legado",
        "23700025         000032000000000000000000000000000000000000000000")
    t_arq_modelo = cfg_ret.get(
        "trailer_arquivo_legado", "23799999         000002000108")
    t_lote, t_arq = _ret._trailers_recalculados(registros, t_lote_modelo, t_arq_modelo)
    r.linhas_finais = registros + [t_lote, t_arq]

    dup_txt = f" ({n_dup} duplicado(s) por filial)" if duplicar_filial else ""
    r.log.append(
        f"{r.total_validos} boleto(s){dup_txt} → {r.total_registros} "
        f"linha(s) no arquivo.")
    if r.colisoes:
        r.log.append(f"⚠ {len(r.colisoes)} valor(es) repetido(s) — ver colisões.")
    if r.primeiro_vencimento:
        r.log.append(f"Vencimentos: {r.primeiro_vencimento} → {r.ultimo_vencimento}")
    return r


def processar(
    planilha_path: Path,
    cfg_ret: Dict[str, Any],
    filiais_por_raiz: Dict[str, List[str]],
    cfg_usinas: Optional[Dict[str, Any]] = None,
    ret_molde_path: Optional[Path] = None,
    ordenar_venc: bool = True,
    duplicar_limite: int = 0,
    duplicar_filial: bool = True,
) -> ResultadoImportacao:
    """
    Fluxo completo. Só precisa da planilha (CSV/XLSX): o molde é embutido.
    `cfg_usinas` traz a regra de corte da NF (mesma do organizador de planilhas de usinas).
    `ret_molde_path` é OPCIONAL — passe um RET só se o leiaute mudar.
    `duplicar_filial` é ON por padrão — a troca de CNPJ por filial é obrigatória (duplicar só
    cria valores repetidos).
    """
    r = ResultadoImportacao()
    if ret_molde_path:
        molde_g, molde_compl = extrair_molde(Path(ret_molde_path))
        r.log.append(f"Molde do leiaute: RET {Path(ret_molde_path).name}")
    else:
        molde_g, molde_compl = DEFAULT_MOLDE_G, DEFAULT_MOLDE_COMPL
        r.log.append("Molde do leiaute: padrão embutido.")

    boletos = ler_planilha(Path(planilha_path), r, cfg_usinas)
    if not boletos:
        r.avisos.append("Nenhum boleto válido na planilha.")
        return r
    return montar(boletos, molde_g, molde_compl, cfg_ret, filiais_por_raiz,
                  ordenar_venc=ordenar_venc, duplicar_limite=duplicar_limite,
                  duplicar_filial=duplicar_filial, r=r)


# Gravação reaproveita o RET (mesmo formato latin-1/CRLF, com divisão opcional)
gravar = _ret.gravar
gravar_dividido = _ret.gravar_dividido
