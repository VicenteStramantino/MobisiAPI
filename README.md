# Mobisi API

API FastAPI do Mobisi, mantendo persistência em JSON e o fluxo de locais e avaliações de acessibilidade.

## Executar

```bash
python -m pip install -r requirements.txt
uvicorn main:app --reload
```

Documentação interativa: `http://127.0.0.1:8000/docs`.

## Escopo

Foram implementados locais, perguntas de acessibilidade e avaliações anônimas. Cadastro/login, usuários, favoritos, banco de dados e autenticação ficaram fora do escopo solicitado.

Cada avaliação deve conter exatamente as 12 perguntas ativas. A nota e os status das quatro dimensões são calculados a partir das respostas, ignorando `NAO_SEI` no denominador.
