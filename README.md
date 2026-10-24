# cnab240-tools

[![tests](https://github.com/Alantustoni/cnab240-tools/actions/workflows/tests.yml/badge.svg)](https://github.com/Alantustoni/cnab240-tools/actions/workflows/tests.yml)

Biblioteca Python para **juntar, validar e montar arquivos bancários
CNAB240** (padrão Febraban de 240 posições), sem interface e sem
dependências externas.

Nasceu dentro da [Contas a Pagar Suite](https://github.com/Alantustoni/contas-a-pagar-suite), um app
de contas a pagar em uso diário num setor financeiro real, e foi separada
para poder ser usada em scripts, pipelines e outros sistemas.

> **English summary** — Pure-Python library for Brazilian CNAB240 bank
> files: merge Bradesco/Itaú collection files (`.RET`, segments G/H/Y/J)
> with recalculated trailers, combine Itaú SISPAG payment returns
> (segments A/B/J, multiple batches), and build segment-G records from a
> boleto spreadsheet using the due-date factor inside the barcode.
> Standard library only. 44 tests. All sample data is synthetic.

## O que faz

| Módulo | Entrada → saída | Destaques |
|---|---|---|
| `cnab240.ret` | vários `.RET` de cobrança → 1 TXT de importação | detecta o banco (237/341) e converte Itaú → Bradesco (H → Y); mantém só registros de detalhe (tipo 3); separa por empresa; filtra ou duplica títulos de usina por CNPJ de filial; ordena por vencimento **sem quebrar o par G + complemento**; **recalcula os trailers** de lote e de arquivo; divide a saída em N partes |
| `cnab240.retorno` | retornos diários de pagamento (SISPAG Itaú) → 1 arquivo | valida header, trailer e pares de lote; rejeita arquivo de cobrança enviado por engano; não mistura bancos nem empresas; renumera os lotes `0001..` e recalcula o trailer |
| `cnab240.importacao_usinas` | planilha de boletos (CSV/XLSX) → TXT segmento G | monta o registro a partir de um molde; tira o vencimento do **fator de vencimento** do código de barras (base Febraban de 22/02/2025); avisa colisões de valor + vencimento; duplica por filial |

Todas as funções devolvem um objeto de resultado (`linhas_finais`, `log`,
`avisos` e contadores) e **não gravam nada** sozinhas — a gravação é
separada (`gravar`, `gravar_dividido`), sempre em latin-1 com CRLF e de
forma atômica.

## Instalação

```bash
pip install .            # só a biblioteca padrão
pip install .[xlsx]      # para ler planilhas .xlsx na importação
```

## Uso

```python
from pathlib import Path
from cnab240 import ret

cfg = {
    "filtro_estrutural": True,      # mantém só registros de detalhe (tipo 3)
    "recalcular_trailer": True,     # trailers calculados pelo conteúdo
    "ordenar_por_vencimento": True,
    "excluir_usinas": False,
    "separar_por_empresa": False,
}
r = ret.processar([Path("bradesco.ret"), Path("itau.ret")], cfg)

print(r.total_titulos, r.convertidos_itau, r.primeiro_vencimento)
for aviso in r.avisos:
    print("atenção:", aviso)

ret.gravar(r, Path("importacao.txt"))
```

Para ver funcionando com arquivos sintéticos gerados na hora:

```bash
python examples/demo.py
```

```
arquivos: 2 | títulos: 3 | convertidos do Itaú: 1
vencimentos: 05/11/2026 a 15/11/2026
valor total: R$ 2.600,40

primeiras colunas de cada linha do arquivo final:
  2370001300002G 010000000000000
  2370001300001G 010000000000000
  2370001300001G 010000000000000
  23700025         0000040000000
  23799999         000001000005
```

O título do Itaú (341) entrou convertido para 237, os títulos saíram
ordenados por vencimento, e os trailers contam 4 registros no lote
(3 títulos + o próprio trailer) e 1 lote / 5 linhas no arquivo.

## Leiaute do segmento G

| Fatia (0-based) | Campo |
|---|---|
| `[0:3]` | Banco (237 Bradesco, 341 Itaú) |
| `[7:8]` | Tipo de registro (0 header, 1 header lote, 3 detalhe, 5 trailer lote, 9 trailer arquivo) |
| `[8:13]` | Sequencial do registro no lote |
| `[13:14]` | Segmento (G, H/Y, J...) |
| `[17:61]` | Código de barras (44 dígitos) |
| `[61:77]` | Tipo de inscrição + CNPJ do favorecido |
| `[77:107]` | Nome do favorecido |
| `[107:115]` | Vencimento (DDMMAAAA) |
| `[115:130]` | Valor (centavos) |
| `[147:162]` | "Seu número" = nº da nota |

## Por que existe

A versão anterior desta rotina colava **dois trailers constantes** no fim do
arquivo, com contadores fixos (`000032`, `000108`), e cortava as linhas com
`[3:-2]` sem validar nada. Quando a quantidade de títulos mudava, o total
saía errado; em arquivos com 3 ou mais lotes, trailers intermediários
ficavam no meio; e o primeiro título de cada arquivo real era descartado.

Aqui os trailers são recalculados a partir do conteúdo, o corte é feito
pela estrutura (tipo de registro) e cada decisão vai para o `log`. O modo
antigo continua disponível (`"recalcular_trailer": False`,
`"filtro_estrutural": False`) para comparação — os 4 testes marcados como
`xfail` documentam justamente esse comportamento legado.

## Testes

```bash
pip install .[dev]
python -m pytest -q
```

44 testes (40 passam, 4 `xfail` de propósito). Todos os dados de teste —
CNPJs, códigos de barras, nomes — são sintéticos.

## Licença

MIT — veja [LICENSE](LICENSE).
