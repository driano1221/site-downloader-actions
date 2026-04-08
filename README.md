# Arquivamento Web — Rio Doce · IPEA

Ferramenta de arquivamento automático de sites relacionados à reparação da Bacia do Rio Doce, desenvolvida pelo **Instituto de Pesquisa Econômica Aplicada (IPEA)**.

---

## O que faz

Para cada site listado, o sistema:

1. **Baixa o conteúdo completo** — páginas HTML, imagens, CSS, JS e PDFs vinculados
2. **Organiza os PDFs** separadamente com índice de metadados (`indice_pdfs.json`)
3. **Gera evidência arquivística** — arquivo WARC (padrão internacional de arquivamento web) com checksums SHA256
4. **Publica os resultados** como assets em um GitHub Release do dia

---

## Como funciona

```
sites.txt  ──►  GitHub Actions (2x/dia)  ──►  Release no GitHub
                  └─ baixa até 3 sites        ├─ site.zip  (snapshot completo)
                     por execução             └─ site__PDFs.zip  (só os PDFs)
```

O workflow roda automaticamente às **6h e 18h UTC** (3h e 15h no horário de Brasília). A cada execução, processa os próximos sites da fila que ainda não foram baixados. O histórico de execuções fica registrado em [`history.md`](./history.md).

### Estrutura de cada snapshot

```
dominio.com__YYYY-MM-DD_HH-MM-SS.zip
└── OFFLINE/          → cópia navegável do site
└── EVIDENCIA/warc/   → arquivo WARC + CDX (evidência)
└── META/             → manifest.json + checksums.sha256
└── LOGS/             → wget.log + app.log
└── _PDFs/            → PDFs organizados + indice_pdfs.json
```

---

## Como adicionar sites

Edite o arquivo [`sites.txt`](./sites.txt) — uma URL por linha. Linhas iniciadas com `#` são ignoradas.
O próximo ciclo automático (ou um disparo manual em **Actions → Run workflow**) processa as URLs novas.

Para reprocessar um site já baixado, remova-o de [`processed_urls.txt`](./processed_urls.txt).

---

## Limites operacionais

| Item | Limite |
|---|---|
| Sites por execução | 3 (configurável) |
| Execuções por dia | 2 (6h e 18h UTC) |
| Tempo máximo por site | 6 horas |
| Tamanho máximo por arquivo | 2 GB (dividido automaticamente se exceder) |

---

## Repositório

| Arquivo | Função |
|---|---|
| `sites.txt` | Lista de sites a arquivar |
| `processed_urls.txt` | Controle de URLs já processadas (atualizado pelo CI) |
| `history.md` | Histórico de execuções com status, duração e tamanho |
| `downloader_cli.py` | Script Python headless de download |
| `site_downloader_v2_4_0.py` | Aplicativo desktop com interface gráfica |
| `.github/workflows/download_sites.yml` | Workflow do GitHub Actions |

---

## Uso local (interface gráfica)

Execute `site_downloader_v2_4_0.py` com Python 3.8+ e wget instalado.  
Na aba **Batch** é possível baixar múltiplos sites em fila, com controle de timeout e quota por site.

---

## Alterações recentes (v2.4.0)

- **Correção crítica:** timeout do batch estava definido incorretamente como ~7 dias; corrigido para 2h por site (configurável)
- **Snapshots parciais salvos automaticamente:** se um download for interrompido por timeout ou pelo usuário, os arquivos já baixados são preservados com manifest, checksums e ABRIR_AQUI.html
- **Novo status `⚠️ parcial`** para distinguir downloads incompletos de erros reais
- **Controles de timeout e quota** na aba Batch — defina quantas horas e quantos MB por site
- **`_index.html` mestre** gerado automaticamente na pasta de destino após cada site, listando todos os snapshots com status, tamanho e links diretos

---

**IPEA** · Instituto de Pesquisa Econômica Aplicada
Projeto de monitoramento e preservação de informações públicas sobre a reparação da Bacia do Rio Doce.
