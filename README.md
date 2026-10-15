# Dataset carburanti per Gasolio Low Cost

Pubblica ogni giorno un file statico con impianti e prezzi dei carburanti in Italia,
ricavato dagli open data del MIMIT, che l'app Android scarica una volta al giorno.

- **Fonte dati:** [MIMIT – Carburanti, prezzi praticati e anagrafica degli impianti](https://www.mimit.gov.it/it/open-data/elenco-dataset/carburanti-prezzi-praticati-e-anagrafica-degli-impianti)
- **Regole di pulizia:** riprese da [Prezzi Carburante](https://github.com/dstmrk/prezzi-carburante) di dstmrk (licenza MIT)

## Come funziona

`build_dataset.py` (solo libreria standard, Python ≥ 3.9) scarica i due CSV MIMIT, li pulisce e scrive:

- `carburanti-v1.txt.gz`: il dataset compresso (~1,3 MB), formato descritto in testa allo script;
- `meta.json`: data di estrazione, conteggi e record scartati, utile per il monitoraggio.

Se il MIMIT pubblica file vuoti o troncati lo script termina con errore e resta online il dataset precedente.

```bash
python3 build_dataset.py public   # genera i file in public/
```

## Pubblicazione

Il workflow `.github/workflows/publish.yml` gira tre volte al giorno e pubblica `public/` su GitHub Pages.

1. Crea un repository GitHub con il contenuto di questa cartella.
2. In *Settings › Pages* scegli **Source: GitHub Actions**.
3. Avvia il workflow da *Actions › Pubblica dataset carburanti › Run workflow*.
4. Il file sarà su `https://<utente>.github.io/<repository>/carburanti-v1.txt.gz`:
   imposta questo URL in `fuelDataUrl` nel `gradle.properties` dell'app.

### Traffico e CDN

Ogni utente scarica il file al massimo una volta al giorno (richieste condizionali con
`If-Modified-Since`). GitHub Pages ha però un limite indicativo di 100 GB al mese,
cioè circa 2.500 utenti attivi ogni giorno. Oltre quella soglia conviene servire `public/`
da **Cloudflare Pages** o **Cloudflare R2**, senza costi di traffico in uscita:
basta sostituire il job `deploy` con la pubblicazione su Cloudflare e aggiornare `fuelDataUrl`.

## Compatibilità

Il nome del file contiene la versione del formato. Una modifica incompatibile va pubblicata
con un nuovo nome (`carburanti-v2.txt.gz`) mantenendo anche il vecchio finché le versioni
dell'app che lo leggono sono in circolazione.
