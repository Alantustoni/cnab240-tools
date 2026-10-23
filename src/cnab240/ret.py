"""
ret.py — Juntador RET (reescrito, com o trailer corrigido).

O que o programa antigo fazia de arriscado (juntador_ret_v2.py):

  1. As 2 linhas finais (trailer de lote e trailer de arquivo, padrão
     CNAB240 Bradesco) eram CONSTANTES com contadores fixos ("000032",
     "000108"). Se a quantidade de registros mudasse, o arquivo saía
     com totais errados.
  2. Cortava linhas[3:-2] às cegas, sem validar se o arquivo tinha a
     estrutura esperada.
  3. O log de conversão (Itaú→Bradesco) era montado mas nunca mostrado.

O que esta versão faz:

  • Valida cada arquivo antes de cortar (tamanho mínimo, banco detectado)
    e devolve um LOG completo por arquivo para quem chama exibir.
  • Trailer RECALCULADO a partir do conteúdo real:
      - trailer de lote  (237 + lote + tipo 5): qtde de registros do lote
        nas posições 18-23;
      - trailer de arquivo (237 + 9999 + tipo 9): qtde de lotes (18-23) e
        qtde total de linhas do arquivo (24-29).
    Os DEMAIS campos são herdados das linhas legadas (mesmo leiaute que o
    banco já aceitava) — só os contadores mudam.
  • Modo legado disponível no config ("recalcular_trailer": false) —
    reproduz exatamente o comportamento antigo se precisar comparar.
  • Renumeração opcional do sequencial dos registros tipo 3 (pos. 9-13),
    desligada por padrão até ser validada com um arquivo aceito pelo banco.

IMPORTANTE: valide o primeiro arquivo gerado com trailer recalculado no
sistema que importa o TXT. Se ele rejeitar, use "recalcular_trailer": false
e anote o motivo — o ajuste é só de posição de campo.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

BANCO_BRADESCO = "BRADESCO"
BANCO_ITAU     = "ITAU"
BANCO_DESCONHECIDO = "DESCONHECIDO"

# ── Layout do SEGMENTO G (título de cobrança, CNAB240) ────────────────
# Posições medidas em arquivos .RET reais de produção (Bradesco e Itaú)
# — idênticas no Itaú (341) e no Bradesco (237):
#   [62:77]   CNPJ do favorecido (15 dígitos, zero à esquerda)
#   [77:107]  nome do favorecido (30)
#   [107:115] vencimento DDMMAAAA
#   [115:130] valor do título em centavos (15)
#   [147:162] "seu número" = Nº DA NOTA (nas usinas: NF + parcela,
#             ex. '0416786201' = NF 04167862 parcela 01)
SEG_G_CNPJ  = slice(62, 77)
SEG_G_NOME  = slice(77, 107)
SEG_G_VENC  = slice(107, 115)
SEG_G_VALOR = slice(115, 130)
SEG_G_DOC   = slice(147, 162)

# Termos que identificam usinas no NOME do favorecido do segmento G —
# mesmos critérios do DDA (config "ret.termos_usinas" pode sobrescrever).
TERMOS_USINAS_DEFAULT = [
    "USIMINAS", "USINAS SIDERURGICAS", "USINA SIDERURGICA",
    "ARCELOR", "MITTAL", "COMPANHIA SIDERURGICA", "SIDERURGICA NACIONAL",
]


def _normalizar_nome(texto: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", texto.upper())
    return t.encode("ascii", "ignore").decode("ascii")


def _e_titulo(linha: str) -> str:
    """Retorna o segmento ('G' ou 'J') se a linha for um título; '' se não."""
    if len(linha) >= 14 and linha[7:8] == "3":
        seg = linha[13:14].upper()
        if seg in ("G", "J"):
            return seg
    return ""


def _venc_da_linha_g(linha: str) -> Optional[datetime]:
    """Lê o vencimento (DDMMAAAA em [107:115]) de uma linha de segmento G."""
    if len(linha) >= 115 and linha[7:8] == "3" and linha[13:14].upper() == "G":
        txt = linha[SEG_G_VENC].strip()
        if txt.isdigit() and len(txt) == 8 and txt != "00000000":
            try:
                return datetime.strptime(txt, "%d%m%Y")
            except ValueError:
                return None
    return None


def _agrupar_blocos(registros: List[str]) -> List[List[str]]:
    """
    Agrupa cada título (segmento G/J) com as suas linhas complementares
    (H/Y…) que vêm logo depois, até o próximo título. Cada bloco é um
    'título completo' — nunca deve ser quebrado ao ordenar ou excluir.
    """
    blocos: List[List[str]] = []
    atual: Optional[List[str]] = None
    for l in registros:
        if _e_titulo(l):
            atual = [l]
            blocos.append(atual)
        elif atual is not None:
            atual.append(l)
        else:                       # linha antes do 1º título (raro)
            atual = [l]
            blocos.append(atual)
    return blocos


def _valor_do_g(linha: str) -> float:
    if _e_titulo(linha) == "G" and len(linha) >= 130:
        try:
            return int(linha[SEG_G_VALOR]) / 100.0
        except ValueError:
            return 0.0
    return 0.0


def _bloco_e_usina(bloco: List[str], termos: List[str]) -> bool:
    """True se o líder do bloco (segmento G) tem nome de usina."""
    lider = bloco[0]
    if _e_titulo(lider) == "G" and len(lider) >= 130:
        nome = _normalizar_nome(lider[SEG_G_NOME])
        return any(t in nome for t in termos)
    return False


def _excluir_blocos_usina(
    registros: List[str], termos: List[str],
) -> Tuple[List[str], int, float]:
    """
    Remove do arquivo final os títulos de usina (bloco G + complementares).
    Devolve (registros_mantidos, qtd_removida, valor_removido).
    """
    mantidos: List[str] = []
    removidos = 0
    valor_removido = 0.0
    for bloco in _agrupar_blocos(registros):
        if _bloco_e_usina(bloco, termos):
            removidos += 1
            valor_removido += _valor_do_g(bloco[0])
        else:
            mantidos.extend(bloco)
    return mantidos, removidos, valor_removido


def _manter_apenas_blocos_usina(
    registros: List[str], termos: List[str],
) -> Tuple[List[str], int, float]:
    """
    INVERSO de _excluir_blocos_usina: mantém no arquivo final SOMENTE os
    títulos de usina (bloco G + complementares), removendo todos os demais
    fornecedores. Devolve (registros_mantidos, qtd_mantida, valor_mantido).
    """
    mantidos: List[str] = []
    qtd = 0
    valor = 0.0
    for bloco in _agrupar_blocos(registros):
        if _bloco_e_usina(bloco, termos):
            qtd += 1
            valor += _valor_do_g(bloco[0])
            mantidos.extend(bloco)
    return mantidos, qtd, valor


def _duplicar_usinas_por_filial(
    registros: List[str],
    filiais_por_raiz: Dict[str, List[str]],
    limite: int = 0,
) -> Tuple[List[str], int, int]:
    """
    Expande um título: MANTÉM a matriz e ACRESCENTA uma cópia por filial.

    O gatilho é a RAIZ do CNPJ do favorecido (8 primeiros dígitos, em [63:71]
    do segmento G): se essa raiz está no cadastro `filiais_por_raiz`, o sistema
    recebe o boleto com o CNPJ da MATRIZ (o original) E também uma cópia por
    CNPJ de filial. Assim, se a nota estiver lançada na matriz (ex.: Usiminas),
    a cópia da matriz casa; se estiver numa filial (ex.: ARMAR/ARCELOR), a
    cópia daquela filial casa. Cobre os dois cenários. Dispara pela RAIZ, não
    pelo nome. Títulos cuja raiz não está no cadastro passam intactos.
    limite>0: só os primeiros N títulos.

    Só o CNPJ muda entre as cópias — número, vencimento, valor e código de
    barras ficam iguais (o sistema casa por CNPJ+valor+vencimento).
    Devolve (registros_novos, titulos_duplicados, linhas_geradas).
    """
    # normaliza o cadastro: raiz(8 díg) -> [cnpj de 14 díg, ...]
    cadastro: Dict[str, List[str]] = {}
    for raiz, lista in (filiais_por_raiz or {}).items():
        rz = re.sub(r"\D", "", str(raiz))[:8]
        filiais = [re.sub(r"\D", "", str(c)) for c in (lista or [])]
        filiais = [c for c in filiais if len(c) == 14]
        if len(rz) == 8 and filiais:
            cadastro[rz] = filiais

    novos: List[str] = []
    titulos_dup = 0
    linhas_geradas = 0
    for bloco in _agrupar_blocos(registros):
        lider = bloco[0]
        if _e_titulo(lider) == "G" and len(lider) >= 77:
            raiz = lider[63:71]
            cnpj_orig = lider[63:77]
            filiais = cadastro.get(raiz)
            if filiais and (limite <= 0 or titulos_dup < limite):
                titulos_dup += 1
                novos.extend(bloco)                 # MANTÉM a matriz (original)
                for cnpj in filiais:
                    if cnpj == cnpj_orig:
                        continue                     # não repete o CNPJ original
                    copia = list(bloco)
                    copia[0] = _substituir(copia[0], 63, cnpj)  # troca CNPJ no G
                    novos.extend(copia)
                    linhas_geradas += len(copia)
                continue
        novos.extend(bloco)
    return novos, titulos_dup, linhas_geradas


def _filtrar_por_vencimento(
    registros: List[str], minimo: datetime,
) -> Tuple[List[str], int]:
    """
    Remove os títulos (bloco G + complementares) cujo vencimento é ANTERIOR
    a `minimo`. Títulos sem data válida são MANTIDOS (não dá para saber se
    venceram). Devolve (registros_mantidos, qtd_removida).
    """
    mantidos: List[str] = []
    removidos = 0
    for bloco in _agrupar_blocos(registros):
        venc = _venc_da_linha_g(bloco[0])
        if venc is not None and venc < minimo:
            removidos += 1
        else:
            mantidos.extend(bloco)
    return mantidos, removidos


def _ordenar_por_vencimento(
    registros: List[str],
) -> Tuple[List[str], Optional[str], Optional[str]]:
    """
    Ordena os títulos por vencimento SEM quebrar os pares de linhas.

    Cada título é uma linha de segmento G seguida das suas complementares
    (H/Y) — que só fazem sentido logo após o G. O algoritmo agrupa cada G
    com as linhas seguintes até o próximo G ("bloco"), ordena os blocos
    pela data do G (ordenação ESTÁVEL: mesmo vencimento mantém a ordem
    original) e remonta a lista. Nenhuma linha é criada ou perdida.
    """
    # MESMO agrupamento da exclusão de usinas e do filtro de vencimento
    # (líder = G ou J). Antes, só G iniciava bloco — um título J virava
    # "carona" do G anterior e viajava com a data dele na ordenação.
    blocos = _agrupar_blocos(registros)

    datas = [_venc_da_linha_g(b[0]) for b in blocos]
    # chave: (sem data?, data) — blocos sem data vão para o fim, ordem estável
    indexados = list(enumerate(blocos))
    indexados.sort(key=lambda par: (
        datas[par[0]] is None,
        datas[par[0]] or datetime.max,
        par[0],                      # desempate estável pela posição original
    ))
    ordenados = [b for _, b in indexados]

    validas = sorted(d for d in datas if d is not None)
    primeiro = f"{validas[0]:%d/%m/%Y}" if validas else None
    ultimo = f"{validas[-1]:%d/%m/%Y}" if validas else None

    plano: List[str] = []
    for b in ordenados:
        plano.extend(b)
    return plano, primeiro, ultimo


@dataclass
class ResultadoRET:
    linhas_finais:  List[str] = field(default_factory=list)
    log:            List[str] = field(default_factory=list)
    avisos:         List[str] = field(default_factory=list)
    total_arquivos: int = 0
    total_registros: int = 0
    total_titulos: int = 0          # todos os títulos (segmentos G/J)
    titulos_usinas: int = 0         # títulos de usinas (pelo nome, seg. G)
    titulos_sem_usinas: int = 0     # o que interessa contar
    valor_titulos: float = 0.0      # soma dos valores (seg. G)
    valor_usinas: float = 0.0
    convertidos_itau: int = 0
    trailer_recalculado: bool = True
    ordenado_venc: bool = False
    primeiro_vencimento: Optional[str] = None   # "DD/MM/AAAA"
    ultimo_vencimento: Optional[str] = None
    usinas_excluidas: bool = False              # usinas removidas do TXT final
    apenas_usinas: bool = False                 # modo inverso: só usinas no TXT
    duplicar_usinas: bool = False               # usinas duplicadas por filial
    titulos_usina_duplicados: int = 0           # nº de títulos de usina expandidos
    linhas_geradas_duplicacao: int = 0          # nº de linhas de usina geradas
    vencimento_minimo: Optional[str] = None     # filtro "a partir de" aplicado
    titulos_vencidos_removidos: int = 0         # títulos antes da data escolhida
    empresa_predominante: Optional[str] = None  # empresa da maioria dos arquivos
    arquivos_fora_empresa: List[str] = field(default_factory=list)  # intrusos

    @property
    def ok(self) -> bool:
        return self.total_registros > 0


def _empresa_do_cabecalho(linhas: List[str], empresas: List[str]) -> Optional[str]:
    """
    Descobre a empresa do arquivo pelo NOME no cabeçalho (header arquivo,
    tipo 0). Casa contra a lista de empresas (ex.: ALFAMETAL, BETAVIARIA),
    ignorando acento. Devolve o nome padronizado (MAIÚSCULAS) ou None.
    """
    termos = [(e, _normalizar_nome(e)) for e in empresas]
    for linha in linhas[:5]:                 # o header costuma ser a 1ª linha
        if len(linha) >= 8 and linha[7:8] == "0":
            alvo = _normalizar_nome(linha)
            for original, termo in termos:
                if termo and termo in alvo:
                    return original.upper()
    return None


def detectar_banco(linhas: List[str]) -> str:
    for linha in linhas:
        if linha.startswith("237"):
            return BANCO_BRADESCO
        if linha.startswith("341"):
            return BANCO_ITAU
    return BANCO_DESCONHECIDO


def converter_itau_para_bradesco(linha: str) -> str:
    """
    341→237 e segmento complementar H (Itaú) → Y (Bradesco).

    A regra herdada do programa antigo ("23700013H"→"23700013Y") supunha a
    letra do segmento no índice 8 — mas no layout real medido nos arquivos
    do Alan o segmento fica no índice 13 (após o sequencial [8:13]), então
    aquela regra NUNCA casava e o H saía sem converter. A conversão agora
    é POSICIONAL (tipo 3 + segmento no índice 13), com a regra antiga
    mantida por compatibilidade com qualquer arquivo naquele formato.
    """
    if linha.startswith("341"):
        linha = "237" + linha[3:]
    if linha.startswith("23700013H"):                  # formato legado
        linha = "23700013Y" + linha[9:]
    elif (len(linha) >= 14 and linha[7:8] == "3"
            and linha[8:13].isdigit() and linha[13:14] == "H"):
        linha = linha[:13] + "Y" + linha[14:]          # layout real (CNAB240)
    return linha


def _separar_por_empresa(arquivos: List[Path], empresas: List[str],
                         r: "ResultadoRET") -> List[Path]:
    """
    Detecta a empresa de cada arquivo (pelo cabeçalho) e mantém só a da
    MAIORIA. Devolve a lista de arquivos a processar. Em caso de empate ou
    de nenhuma empresa reconhecida, não remove nada (só registra aviso).
    Preenche r.empresa_predominante e r.arquivos_fora_empresa.
    """
    from collections import Counter

    detectada: Dict[Path, Optional[str]] = {}
    for arq in arquivos:
        try:
            texto = Path(arq).read_text(encoding="latin-1", errors="ignore")
            linhas = [l.rstrip("\r\n") for l in texto.splitlines()]
            detectada[arq] = _empresa_do_cabecalho(linhas, empresas)
        except OSError:
            detectada[arq] = None

    sem_empresa = [Path(a).name for a, e in detectada.items() if e is None]
    if sem_empresa:
        r.avisos.append(
            "Empresa não identificada no cabeçalho de: "
            + "; ".join(sem_empresa) + " — arquivo(s) MANTIDOS. Confira.")

    contagem = Counter(e for e in detectada.values() if e)
    if len(contagem) < 2:
        return arquivos                      # todos da mesma empresa (ou nenhuma)

    ordenado = contagem.most_common()
    if ordenado[0][1] == ordenado[1][1]:     # EMPATE — não decide sozinho
        resumo = ", ".join(f"{e}: {n}" for e, n in ordenado)
        r.avisos.append(
            f"Empate na detecção de empresa ({resumo}). Nenhum arquivo foi "
            "removido — confira se não misturou empresas.")
        return arquivos

    predominante = ordenado[0][0]
    r.empresa_predominante = predominante
    mantidos, removidos = [], []
    for arq in arquivos:
        emp = detectada.get(arq)
        if emp is None or emp == predominante:
            mantidos.append(arq)             # mantém a maioria e os indefinidos
        else:
            removidos.append(arq)
            r.arquivos_fora_empresa.append(f"{Path(arq).name} ({emp})")
    if removidos:
        r.avisos.append(
            f"Empresa predominante: {predominante}. "
            f"{len(removidos)} arquivo(s) de outra empresa REMOVIDOS: "
            + "; ".join(r.arquivos_fora_empresa))
        r.log.append(
            f"• Separação por empresa: mantida {predominante}; "
            f"removidos {len(removidos)} intruso(s).")
    return mantidos


def processar(arquivos: List[Path], cfg_ret: Dict[str, Any],
              termos_usina_extra: Optional[List[str]] = None,
              vencimento_minimo: Optional[datetime] = None) -> ResultadoRET:
    """
    Junta os arquivos RET em uma lista única de registros + trailers.
    Não grava nada — a gravação é de quem chama (separação testável).

    termos_usina_extra: termos adicionais para reconhecer usinas (ex.: os
    grupos de usina definidos pelo usuário), somados a termos_usinas.
    vencimento_minimo: se informado, os títulos com vencimento ANTES dessa
    data ficam de fora do arquivo final.
    """
    n_cab  = int(cfg_ret.get("linhas_cabecalho", 3))
    n_rod  = int(cfg_ret.get("linhas_rodape", 2))
    prefixos_ignorados = tuple(cfg_ret.get("prefixos_ignorados",
                                           ["23700015", "23700021I03"]))
    # Filtro ESTRUTURAL (novo padrão): mantém apenas registros de detalhe
    # (tipo 3 na posição 8) e descarta headers/trailers de QUALQUER lote.
    # Corrige dois problemas do corte posicional antigo [3:-2]:
    #   1. a 3ª linha dos arquivos reais JÁ É um título — o corte antigo
    #      comia o primeiro título de cada arquivo;
    #   2. arquivos com 3+ lotes deixavam trailers intermediários no meio.
    filtro_estrutural = bool(cfg_ret.get("filtro_estrutural", True))
    recalcular = bool(cfg_ret.get("recalcular_trailer", True))
    renumerar  = bool(cfg_ret.get("renumerar_sequencial", False))
    ordenar_venc = bool(cfg_ret.get("ordenar_por_vencimento", True))
    # padrão True (mesmo da ALAN Suite)
    excluir_usinas = bool(cfg_ret.get("excluir_usinas", True))
    # MODO SÓ USINAS (inverso): mantém apenas os títulos de usina e remove
    # todos os outros fornecedores. Tem PRECEDÊNCIA sobre excluir_usinas
    # (são opostos — não faz sentido os dois ao mesmo tempo).
    somente_usinas = bool(cfg_ret.get("somente_usinas", False))
    # DUPLICAR USINAS POR FILIAL: cada título de usina vira uma cópia por
    # CNPJ de filial (do cadastro filiais_por_raiz), para o sistema vincular
    # o boleto por CNPJ+título+vencimento sem depender da raiz. Quando ligado,
    # as usinas NÃO são excluídas (ficam, duplicadas). limite>0 = só as
    # primeiras N usinas (modo teste).
    duplicar_usinas = bool(cfg_ret.get("duplicar_usinas_por_filial", False))
    filiais_por_raiz = cfg_ret.get("filiais_por_raiz", {}) or {}
    duplicar_limite = int(cfg_ret.get("duplicar_limite_teste", 0) or 0)
    separar_por_empresa = bool(cfg_ret.get("separar_por_empresa", True))
    empresas_ret = list(cfg_ret.get("empresas_ret", ["ALFAMETAL", "BETAVIARIA"]))
    trailer_lote_legado    = cfg_ret.get(
        "trailer_lote_legado",
        "23700025         000032000000000000000000000000000000000000000000")
    trailer_arquivo_legado = cfg_ret.get(
        "trailer_arquivo_legado", "23799999         000002000108")

    r = ResultadoRET(trailer_recalculado=recalcular)
    registros: List[str] = []

    # ── separa por empresa: mantém a MAIORIA, retira os arquivos intrusos ──
    # (evita juntar, por engano, um arquivo da Alfametal num lote da
    #  Betaviaria — ou vice-versa). Empate: não remove nada, só avisa.
    if separar_por_empresa and len(arquivos) > 1:
        arquivos = _separar_por_empresa(arquivos, empresas_ret, r)

    for arquivo in arquivos:
        r.total_arquivos += 1
        try:
            texto = Path(arquivo).read_text(encoding="latin-1", errors="ignore")
        except OSError as exc:
            r.avisos.append(f"{Path(arquivo).name}: não foi possível ler ({exc})")
            continue

        linhas = [l.rstrip("\r\n") for l in texto.splitlines()]
        banco = detectar_banco(linhas)

        # ── validação ANTES do corte (o antigo cortava às cegas) ──────
        if banco == BANCO_DESCONHECIDO:
            r.avisos.append(
                f"{Path(arquivo).name}: banco não identificado — arquivo IGNORADO.")
            r.log.append(f"✖ {Path(arquivo).name} — banco não identificado (ignorado)")
            continue
        if filtro_estrutural:
            # validação por CONTEÚDO: precisa ter ao menos 1 detalhe (tipo 3)
            # — um arquivo válido de 1 título tem só 5 linhas e passaria a
            # ser rejeitado pela contagem posicional abaixo
            if not any(l[7:8] == "3" for l in linhas if l.strip()):
                r.avisos.append(
                    f"{Path(arquivo).name}: nenhum registro de detalhe "
                    "(tipo 3) — arquivo IGNORADO.")
                r.log.append(f"✖ {Path(arquivo).name} — sem detalhes (ignorado)")
                continue
        elif len(linhas) <= n_cab + n_rod:
            r.avisos.append(
                f"{Path(arquivo).name}: só tem {len(linhas)} linhas "
                f"(mínimo {n_cab + n_rod + 1}) — arquivo IGNORADO.")
            r.log.append(f"✖ {Path(arquivo).name} — estrutura curta demais (ignorado)")
            continue

        if banco == BANCO_ITAU:
            linhas = [converter_itau_para_bradesco(l) for l in linhas]
            r.convertidos_itau += 1

        if filtro_estrutural:
            corpo = [l for l in linhas if l.strip()]
            mantidas = [l for l in corpo if l[7:8] == "3"]
            descartadas = len(corpo) - len(mantidas)
            detalhe_corte = (f"{descartadas} headers/trailers descartados "
                             f"(filtro estrutural)")
        else:
            corpo = linhas[n_cab:len(linhas) - n_rod]
            mantidas, descartadas = _filtrar(corpo, prefixos_ignorados)
            detalhe_corte = (f"cortadas {n_cab}+{n_rod} posicionais"
                             + (f" · {descartadas} por prefixo"
                                if descartadas else ""))
            # aviso: o corte posicional antigo pode comer registro de detalhe
            # (no início E no fim — arquivo sem os 2 trailers perde títulos)
            cortadas = linhas[:n_cab] + (linhas[-n_rod:] if n_rod else [])
            if any(l[7:8] == "3" for l in cortadas):
                r.avisos.append(
                    f"{Path(arquivo).name}: o corte posicional descartou "
                    "linha(s) de DETALHE no início/fim — provavelmente um "
                    "título foi perdido. Ative o filtro estrutural nas "
                    "Configurações.")
        registros.extend(mantidas)

        r.log.append(
            f"✔ {Path(arquivo).name} — {banco}"
            + (" (convertido → BRADESCO)" if banco == BANCO_ITAU else "")
            + f" · {len(mantidas)} registros mantidos · {detalhe_corte}"
        )

    # ── Contagem de títulos (feita ANTES de excluir/ordenar) ──────────
    # Cada título = 1 registro de detalhe segmento G (par com H/Y).
    # Usinas são reconhecidas pelo NOME do favorecido (posições 78-107),
    # usando os termos do RET + os grupos extras informados por quem chama
    # Usinas (termos_usina_extra) — assim "ARMAR", "SOLUÇÕES" etc. contam.
    termos = [
        _normalizar_nome(t)
        for t in list(cfg_ret.get("termos_usinas", TERMOS_USINAS_DEFAULT))
        + list(termos_usina_extra or [])
    ]
    valores_ilegiveis = 0
    for linha in registros:
        seg = _e_titulo(linha)
        if not seg:
            continue
        r.total_titulos += 1
        if seg == "G" and len(linha) >= 130:
            nome = _normalizar_nome(linha[SEG_G_NOME])
            valor = _valor_do_g(linha)
            if valor == 0.0 and not linha[SEG_G_VALOR].strip().isdigit():
                valores_ilegiveis += 1        # campo de valor malformado
            r.valor_titulos += valor
            if any(t in nome for t in termos):
                r.titulos_usinas += 1
                r.valor_usinas += valor
    if valores_ilegiveis:
        r.avisos.append(
            f"{valores_ilegiveis} título(s) com campo de VALOR ilegível — "
            "somados como R$ 0,00 nos totais (o arquivo em si não é alterado).")
    r.titulos_sem_usinas = r.total_titulos - r.titulos_usinas
    if r.total_titulos:
        r.log.append(
            f"• Títulos: {r.total_titulos} no total — "
            f"{r.titulos_usinas} de usinas · "
            f"{r.titulos_sem_usinas} contáveis (sem usinas)")

    # ── Modo SÓ USINAS (inverso) — precede a exclusão ─────────────────
    if somente_usinas and registros:
        registros, n_keep, val_keep = _manter_apenas_blocos_usina(registros, termos)
        r.apenas_usinas = True
        r.log.append(
            f"• MODO SÓ USINAS: mantidos {n_keep} título(s) de usina "
            f"(R$ {val_keep:,.2f}); os demais fornecedores foram removidos.")
    # ── Ignorar (excluir) as notas de usina do arquivo final ──────────
    # (não exclui quando o modo é DUPLICAR — aí as usinas ficam, expandidas)
    elif excluir_usinas and not duplicar_usinas and registros:
        registros, n_rem, val_rem = _excluir_blocos_usina(registros, termos)
        r.usinas_excluidas = True
        r.log.append(
            f"• {n_rem} título(s) de USINA IGNORADOS — não entram no TXT "
            f"final (R$ {val_rem:,.2f}).")

    # ── Duplicar usinas por filial (para vincular por CNPJ) ───────────
    if duplicar_usinas and registros:
        registros, n_dup, n_lin = _duplicar_usinas_por_filial(
            registros, filiais_por_raiz, duplicar_limite)
        r.duplicar_usinas = True
        r.titulos_usina_duplicados = n_dup
        r.linhas_geradas_duplicacao = n_lin
        extra = f" (limite de teste: {duplicar_limite})" if duplicar_limite else ""
        r.log.append(
            f"• DUPLICAÇÃO por filial: {n_dup} título(s) de usina expandido(s) "
            f"em {n_lin} linha(s), uma por CNPJ de filial{extra}.")
        if duplicar_usinas and n_dup == 0:
            r.avisos.append(
                "Duplicação ligada, mas nenhum título de usina tinha filiais "
                "cadastradas (confira o cadastro de CNPJs por raiz).")

    # Filtro por vencimento (o Alan escolhe a data inicial): remove os
    # títulos já vencidos/antes da data escolhida.
    if vencimento_minimo is not None and registros:
        registros, n_venc = _filtrar_por_vencimento(registros, vencimento_minimo)
        r.vencimento_minimo = vencimento_minimo.strftime("%d/%m/%Y")
        r.titulos_vencidos_removidos = n_venc
        r.log.append(
            f"• {n_venc} título(s) com vencimento ANTES de "
            f"{r.vencimento_minimo} removidos (fora do TXT).")

    # Ordena por vencimento ANTES de renumerar (para o sequencial seguir a
    # nova ordem) e de recalcular o trailer (que só depende da contagem).
    if ordenar_venc and registros:
        registros, r.primeiro_vencimento, r.ultimo_vencimento = (
            _ordenar_por_vencimento(registros))
        r.ordenado_venc = True
        if r.primeiro_vencimento:
            r.log.append(
                f"• Ordenado por vencimento: {r.primeiro_vencimento} → "
                f"{r.ultimo_vencimento}")

    if renumerar:
        registros = _renumerar_sequencial(registros)
        r.log.append(f"• Sequencial renumerado: 1 → {len(registros)}")

    r.total_registros = len(registros)

    if not registros:
        r.avisos.append("Nenhum registro válido — arquivo final NÃO deve ser gerado.")
        return r

    # ── trailers ───────────────────────────────────────────────────────
    if recalcular:
        t_lote, t_arq = _trailers_recalculados(
            registros, trailer_lote_legado, trailer_arquivo_legado)
        r.log.append(
            f"• Trailer RECALCULADO — lote: {len(registros) + 1:06d} registros | "
            f"arquivo: {len(registros) + 2:06d} linhas"
        )
    else:
        t_lote, t_arq = trailer_lote_legado, trailer_arquivo_legado
        r.log.append("• Trailer em MODO LEGADO (linhas fixas do programa antigo)")
        r.avisos.append(
            "Modo legado: os contadores do trailer são fixos e podem não "
            "bater com a quantidade real de registros.")

    r.linhas_finais = registros + [t_lote, t_arq]
    return r


def gravar(resultado: ResultadoRET, arquivo_saida: Path) -> None:
    """
    Grava o TXT final em latin-1 com quebra CRLF (padrão bancário).
    Escrita ATÔMICA: gera em .tmp no mesmo diretório e faz os.replace —
    o arquivo final nunca fica meio-escrito se o processo cair no meio.
    """
    conteudo = "\r\n".join(resultado.linhas_finais) + "\r\n"
    saida = Path(arquivo_saida)
    fd, tmp = tempfile.mkstemp(dir=saida.parent, suffix=".tmp.txt")
    try:
        with os.fdopen(fd, "w", encoding="latin-1", newline="") as f:
            f.write(conteudo)
        os.replace(tmp, saida)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    logger.info("RET: gravado %s (%d linhas)",
                arquivo_saida, len(resultado.linhas_finais))


def _dividir_registros_em_partes(registros: List[str], n: int) -> List[List[str]]:
    """
    Divide os registros em até `n` partes de tamanho parecido, SEM quebrar
    blocos (cada título G/J + complementares fica inteiro numa parte só).
    """
    if n <= 1 or not registros:
        return [list(registros)]
    blocos = _agrupar_blocos(registros)
    total = len(registros)
    alvo = total / n
    partes: List[List[str]] = [[] for _ in range(n)]
    consumido = 0
    for b in blocos:
        # a parte é escolhida pela posição de INÍCIO do bloco (linhas já
        # consumidas / alvo) — garante n partes de tamanho parecido sem
        # quebrar blocos e sem "estourar" o limiar como na versão anterior.
        idx = min(n - 1, int(consumido / alvo)) if alvo > 0 else n - 1
        partes[idx].extend(b)
        consumido += len(b)
    return [p for p in partes if p]        # descarta partes vazias (poucos blocos)


def gravar_dividido(resultado: ResultadoRET, arquivo_saida: Path,
                    n_partes: int) -> List[Path]:
    """
    Grava a saída em `n_partes` arquivos independentes, cada um com seus
    próprios trailers RECALCULADOS (contagem por parte) e com os títulos
    inteiros (G+complementares nunca são separados). Útil quando o sistema
    de destino tem limite de registros por importação.

    Retorna a lista de caminhos gerados (…_parte1deN.txt, …_parte2deN.txt…).
    Com n_partes<=1, grava um arquivo só (igual a gravar()).
    """
    saida = Path(arquivo_saida)
    if n_partes <= 1:
        gravar(resultado, saida)
        return [saida]

    registros = list(resultado.linhas_finais[:-2])   # tira os 2 trailers
    t_lote_modelo = resultado.linhas_finais[-2]
    t_arq_modelo  = resultado.linhas_finais[-1]

    partes = _dividir_registros_em_partes(registros, n_partes)
    total_partes = len(partes)
    caminhos: List[Path] = []

    for i, parte in enumerate(partes, 1):
        # trailers recalculados PARA ESTA PARTE (mesmo leiaute, só a contagem)
        t_lote = _substituir(t_lote_modelo, 17, f"{len(parte) + 1:06d}")
        t_arq  = _substituir(t_arq_modelo, 17, "000001")
        t_arq  = _substituir(t_arq, 23, f"{len(parte) + 2:06d}")
        linhas = parte + [t_lote, t_arq]

        destino = saida.with_name(
            f"{saida.stem}_parte{i}de{total_partes}{saida.suffix}")
        conteudo = "\r\n".join(linhas) + "\r\n"
        fd, tmp = tempfile.mkstemp(dir=destino.parent, suffix=".tmp.txt")
        try:
            with os.fdopen(fd, "w", encoding="latin-1", newline="") as f:
                f.write(conteudo)
            os.replace(tmp, destino)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        caminhos.append(destino)
        logger.info("RET: gravada parte %d/%d — %s (%d linhas)",
                    i, total_partes, destino.name, len(linhas))

    return caminhos


# ---------------------------------------------------------------------- #
# internos                                                                #
# ---------------------------------------------------------------------- #
def _filtrar(corpo: List[str], prefixos: Tuple[str, ...]) -> Tuple[List[str], int]:
    mantidas: List[str] = []
    descartadas = 0
    for linha in corpo:
        texto = linha.strip()
        if not texto:
            continue
        if any(texto.startswith(p) for p in prefixos):
            descartadas += 1
            continue
        mantidas.append(linha)
    return mantidas, descartadas


def _renumerar_sequencial(registros: List[str]) -> List[str]:
    """
    Renumera o nº sequencial do registro no lote (CNAB240: posições 9-13,
    índice 8:13) apenas em registros de detalhe (tipo 3 na posição 8).
    """
    out: List[str] = []
    seq = 0
    for linha in registros:
        # só reescreve quando [8:13] é mesmo um sequencial numérico —
        # proteção contra layouts com a letra do segmento nessa posição
        if len(linha) >= 13 and linha[7:8] == "3" and linha[8:13].isdigit():
            seq += 1
            linha = linha[:8] + f"{seq:05d}" + linha[13:]
        out.append(linha)
    return out


def _trailers_recalculados(
    registros: List[str], modelo_lote: str, modelo_arquivo: str,
) -> Tuple[str, str]:
    """
    Reescreve APENAS os contadores nos trailers, preservando o restante
    do leiaute das linhas legadas (que o banco já aceitava):

      trailer de lote   (…5): pos 18-23  = registros do lote
                               (detalhes + o próprio trailer)
      trailer de arquivo (…9): pos 18-23 = qtde de lotes (000001)
                               pos 24-29 = total de linhas do arquivo
    """
    qtd_lote    = len(registros) + 1        # detalhes + trailer de lote
    qtd_arquivo = len(registros) + 2        # + trailer de arquivo

    t_lote = _substituir(modelo_lote, 17, f"{qtd_lote:06d}")
    t_arq  = _substituir(modelo_arquivo, 17, "000001")
    t_arq  = _substituir(t_arq, 23, f"{qtd_arquivo:06d}")
    return t_lote, t_arq


def _substituir(linha: str, inicio: int, texto: str) -> str:
    fim = inicio + len(texto)
    if len(linha) < fim:
        linha = linha.ljust(fim)
    return linha[:inicio] + texto + linha[fim:]
