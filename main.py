# """API REST do Mobisi, com persistência simples em JSON."""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "dados.json"
DATA_LOCK = Lock()

DIMENSIONS = [
    ("FISICA", "Física"),
    ("VISUAL", "Visual"),
    ("AUDITIVA", "Auditiva"),
    ("COGNITIVA", "Cognitiva"),
]

QUESTIONS = [
    (1, "FISICA", "A entrada pode ser acessada sem degraus ou possui rampa/elevador?", "SIM"),
    (2, "FISICA", "Existe vaga, embarque ou acesso próximo para pessoas com deficiência?", "SIM"),
    (3, "FISICA", "A porta tem passagem confortável para cadeira de rodas, andador ou carrinho?", "SIM"),
    (4, "VISUAL", "Existe piso tátil direcional e de alerta onde necessário, como entrada, obstáculos e mudanças de nível?", "SIM"),
    (5, "VISUAL", "Existem obstáculos suspensos, objetos no caminho ou mudanças de piso sem sinalização?", "NAO"),
    (6, "VISUAL", "Existem placas em Braille?", "SIM"),
    (7, "AUDITIVA", "O local possui atendimento por texto, escrita ou mensagem?", "SIM"),
    (8, "AUDITIVA", "Existe atendimento em Libras ou possibilidade de intérprete?", "SIM"),
    (9, "AUDITIVA", "Vídeos e conteúdos possuem legendas?", "SIM"),
    (10, "COGNITIVA", "Existe um espaço mais silencioso para descanso ou espera?", "SIM"),
    (11, "COGNITIVA", "O ambiente possui cheiros, sons ou estímulos visuais muito intensos?", "NAO"),
    (12, "COGNITIVA", "A equipe respeita diferentes formas de comportamento e comunicação?", "SIM"),
]
QUESTION_MAP = {item[0]: item for item in QUESTIONS}


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def initial_data() -> dict:
    return {"locais": [], "avaliacoes": [], "next_local_id": 1, "next_avaliacao_id": 1}


def load_data() -> dict:
    raw = read_storage_data()
    # Compatibilidade com o locais.json antigo, que era uma lista de locais.
    if isinstance(raw, list):
        locais = []
        for old in raw:
            endereco = old.get("Endereco", {})
            locais.append({
                "id": old.get("id"),
                "nome": old.get("Nome", ""),
                "categoria": old.get("Categoria"),
                "endereco": ", ".join(filter(None, [
                    endereco.get("RuaNumero"), endereco.get("Bairro"),
                    endereco.get("Cidade"), endereco.get("Estado"),
                ])),
                "telefone": None,
                "site_url": None,
                "imagem_url": None,
                "is_active": True,
                "criado_em": now(),
                "atualizado_em": now(),
                "notas_historicas": old.get("Nota") or [],
                "acessibilidade_historica": {
                    key: value for key, value in old.items()
                    if key in {"Rampa", "Banheiro_acessivel", "Piso_tatil", "Elevador", "Libras"}
                },
            })
        data = {"locais": locais, "avaliacoes": [],
                "next_local_id": max([x["id"] for x in locais] or [0]) + 1,
                "next_avaliacao_id": 1}
        save_data(data)
        return data
    # Recalcula os contadores e corrige IDs repetidos após edições manuais no JSON.
    changed = False
    used_local_ids = set()
    next_id = max((int(item.get("id", 0)) for item in raw.get("locais", [])
                   if str(item.get("id", "")).isdigit()), default=0) + 1
    for item in raw.get("locais", []):
        item_id = item.get("id")
        if not isinstance(item_id, int) or item_id in used_local_ids:
            item["id"] = next_id
            used_local_ids.add(next_id)
            next_id += 1
            changed = True
        else:
            used_local_ids.add(item_id)
    if raw.get("next_local_id") != next_id:
        raw["next_local_id"] = next_id
        changed = True

    used_review_ids = set()
    next_review_id = max((int(item.get("id", 0)) for item in raw.get("avaliacoes", [])
                          if str(item.get("id", "")).isdigit()), default=0) + 1
    for item in raw.get("avaliacoes", []):
        item_id = item.get("id")
        if not isinstance(item_id, int) or item_id in used_review_ids:
            item["id"] = next_review_id
            used_review_ids.add(next_review_id)
            next_review_id += 1
            changed = True
        else:
            used_review_ids.add(item_id)
    if raw.get("next_avaliacao_id") != next_review_id:
        raw["next_avaliacao_id"] = next_review_id
        changed = True
    if changed:
        save_data(raw)
    return raw


class StoredData(dict):
    """JSON document plus its storage version (not serialized)."""
    etag: str | None = None


def remote_storage_enabled() -> bool:
    return bool(os.getenv("BLOB_STORE_ID") or os.getenv("BLOB_READ_WRITE_TOKEN"))


def blob_session(etag: str | None = None):
    # These two protocol options are available in the official JS SDK but not
    # yet exposed by Python 0.5: origin reads and conditional uploads.
    import httpx2
    from vercel.api import session

    def prepare_request(request):
        if request.method == "GET" and request.url.host.endswith(".private.blob.vercel-storage.com"):
            request.url = request.url.copy_set_param("cache", "0")
        if etag and request.method == "PUT" and request.url.host == "vercel.com":
            request.headers["x-if-match"] = etag

    def check_response(response):
        if response.status_code in (409, 412):
            raise HTTPException(409, detail={
                "codigo": "CONFLITO_DE_ATUALIZACAO",
                "mensagem": "Os dados foram alterados por outra requisicao. Recarregue e tente novamente.",
            })

    return session(httpx_client_factory=lambda: httpx2.Client(
        timeout=30.0,
        event_hooks={"request": [prepare_request], "response": [check_response]},
    ))


def read_storage_data() -> dict:
    if remote_storage_enabled():
        from vercel.blob import BlobNotFoundError, BlobError
        from vercel.blob import sync as blob
        try:
            with blob_session():
                result = blob.get_blob("mobisi/dados.json", access="private")
            data = StoredData(json.loads(result.body))
            data.etag = result.metadata.etag
            if not data.etag:
                raise HTTPException(503, detail="Armazenamento retornou dados sem versao.")
            return data
        except BlobNotFoundError:
            # Seed only when the remote file does not exist. Never replace an
            # existing remote document with the bundled JSON on a new deploy.
            with DATA_FILE.open("r", encoding="utf-8") as file:
                data = StoredData(json.load(file))
            save_data(data)
            return data
        except BlobError as error:
            raise HTTPException(503, detail="Armazenamento temporariamente indisponivel.") from error
    if os.getenv("VERCEL"):
        raise HTTPException(503, detail="Conecte um Vercel Blob privado ao projeto.")
    if not DATA_FILE.exists():
        data = initial_data()
        save_data(data)
        return data
    with DATA_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_data(data: dict) -> None:
    if remote_storage_enabled():
        from vercel.blob import BlobError
        from vercel.blob import sync as blob
        etag = getattr(data, "etag", None)
        try:
            with blob_session(etag):
                result = blob.put_blob(
                    "mobisi/dados.json",
                    json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
                    access="private",
                    content_type="application/json",
                    add_random_suffix=False,
                    allow_overwrite=etag is not None,
                    cache_control_max_age=60,
                )
            if isinstance(data, StoredData):
                data.etag = result.etag
            return
        except BlobError as error:
            raise HTTPException(503, detail="Nao foi possivel salvar os dados. Tente novamente.") from error
    if os.getenv("VERCEL"):
        raise HTTPException(503, detail="Conecte um Vercel Blob privado ao projeto.")
    temp = DATA_FILE.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    temp.replace(DATA_FILE)


def get_local(data: dict, local_id: int) -> dict:
    local = next((x for x in data["locais"] if x["id"] == local_id and x["is_active"]), None)
    if not local:
        raise HTTPException(404, detail={"codigo": "LOCAL_NAO_ENCONTRADO", "mensagem": "Local não encontrado."})
    return local


def pagination(total: int, page: int, limit: int) -> dict:
    return {"pagina": page, "limite": limit, "total": total,
            "total_paginas": math.ceil(total / limit) if total else 0}


class LocalInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    nome: str = Field(min_length=1, max_length=160)
    endereco: str = Field(min_length=1)
    categoria: str | None = Field(default=None, max_length=80)
    telefone: str | None = Field(default=None, max_length=30)
    site_url: HttpUrl | None = None
    imagem_url: HttpUrl | None = None


class LocalUpdate(BaseModel):
    nome: str | None = Field(default=None, min_length=1, max_length=160)
    endereco: str | None = Field(default=None, min_length=1)
    categoria: str | None = Field(default=None, max_length=80)
    telefone: str | None = Field(default=None, max_length=30)
    site_url: HttpUrl | None = None
    imagem_url: HttpUrl | None = None


class AnswerInput(BaseModel):
    pergunta_id: int = Field(gt=0)
    resposta: Literal["SIM", "NAO", "NAO_SEI"]


class ReviewInput(BaseModel):
    respostas: list[AnswerInput] = Field(min_length=1)


def validate_answers(respostas: list[AnswerInput]) -> None:
    ids = [answer.pergunta_id for answer in respostas]
    active_ids = set(QUESTION_MAP)
    if len(ids) != len(set(ids)):
        raise HTTPException(400, detail={"codigo": "PERGUNTA_REPETIDA", "mensagem": "Não pode haver pergunta repetida."})
    if set(ids) != active_ids:
        raise HTTPException(400, detail={"codigo": "RESPOSTAS_INCOMPLETAS", "mensagem": "Envie exatamente as 12 perguntas ativas."})


def review_output(review: dict) -> dict:
    return {"id": review["id"], "respostas": review["respostas"],
            "criada_em": review["criada_em"], "atualizada_em": review["atualizada_em"]}


def accessibility_summary(data: dict, local_id: int) -> tuple[float | None, int, list[dict]]:
    local = next(x for x in data["locais"] if x["id"] == local_id)
    reviews = [r for r in data["avaliacoes"] if r["local_id"] == local_id]
    legacy_scores = local.get("notas_historicas", [])
    counts = {code: {"sim": 0, "nao": 0, "nao_sei": 0} for code, _ in DIMENSIONS}
    positive = valid = 0
    for review in reviews:
        for answer in review["respostas"]:
            question = QUESTION_MAP[answer["pergunta_id"]]
            code, accessible = question[1], question[3]
            value = answer["resposta"].lower()
            counts[code][value] += 1
            if value != "nao_sei":
                valid += 1
                positive += value.upper() == accessible
        
    dimensions = []
    for code, name in DIMENSIONS:
        item = counts[code]
        denominator = item["sim"] + item["nao"]
        percentage = item["sim"] / denominator if denominator else None
        if percentage is None:
            state = "NAO_AVALIADA"
        elif percentage < 0.5:
            state = "PRECISA_MELHORAR"
        elif percentage < 0.8:
            state = "PARCIAL"
        else:
            state = "ACESSIVEL"
        dimensions.append({"dimensao": code, "nome": name, "status": state,
                           "percentual": percentage, "sim": item["sim"],
                           "nao": item["nao"], "nao_sei": item["nao_sei"]})
    score = round(positive / valid * 5, 1) if valid else (
        round(sum(legacy_scores) / len(legacy_scores), 1) if legacy_scores else None
    )
    return (score, len(reviews) + len(legacy_scores), dimensions)


app = FastAPI(title="Mobisi API", version="1.0.0", description="API de locais e acessibilidade do Mobisi")


@app.get("/api/v1/locais")
def list_places(busca: str | None = None, categoria: str | None = None,
                pagina: int = Query(1, ge=1), limite: int = Query(12, ge=1, le=50)):
    with DATA_LOCK:
        data = load_data()
        items = [x for x in data["locais"] if x["is_active"]]
        if busca:
            term = busca.casefold()
            items = [x for x in items if term in x["nome"].casefold() or term in x["endereco"].casefold()]
        if categoria:
            items = [x for x in items if (x.get("categoria") or "").casefold() == categoria.casefold()]
        total = len(items)
        start = (pagina - 1) * limite
        result = []
        for local in items[start:start + limite]:
            note, reviews, _ = accessibility_summary(data, local["id"])
            result.append({"id": local["id"], "nome": local["nome"], "categoria": local.get("categoria"), "endereco_resumido": local["endereco"],
                           "imagem_url": local["imagem_url"], "nota": note, "total_avaliacoes": reviews})
        return {"itens": result, "paginacao": pagination(total, pagina, limite)}


@app.post("/api/v1/locais", status_code=201)
def create_place(payload: LocalInput):
    with DATA_LOCK:
        data = load_data(); stamp = now()
        next_id = max((item["id"] for item in data["locais"]), default=0) + 1
        data["next_local_id"] = next_id + 1
        local = {"id": next_id, **payload.model_dump(mode="json"), "is_active": True,
                 "criado_em": stamp, "atualizado_em": stamp}
        data["locais"].append(local); save_data(data)
        return local_detail(data, local)


def local_detail(data: dict, local: dict) -> dict:
    note, reviews, dimensions = accessibility_summary(data, local["id"])
    return {**local, "nota": note, "total_avaliacoes": reviews, "acessibilidade": dimensions,
            "notas_historicas": local.get("notas_historicas", []),
            "acessibilidade_historica": local.get("acessibilidade_historica", {})}


@app.get("/api/v1/locais/{local_id}")
def get_place(local_id: int):
    with DATA_LOCK:
        data = load_data(); return local_detail(data, get_local(data, local_id))


@app.put("/api/v1/locais/{local_id}")
def update_place(local_id: int, payload: LocalUpdate):
    with DATA_LOCK:
        data = load_data(); local = get_local(data, local_id)
        for key, value in payload.model_dump(exclude_unset=True, mode="json").items(): local[key] = value
        local["atualizado_em"] = now(); save_data(data); return local_detail(data, local)


@app.delete("/api/v1/locais/{local_id}", status_code=204)
def delete_place(local_id: int):
    with DATA_LOCK:
        data = load_data(); local = get_local(data, local_id); local["is_active"] = False; local["atualizado_em"] = now(); save_data(data)
        return Response(status_code=204)


@app.get("/api/v1/acessibilidade/perguntas")
def get_questions():
    return {"dimensoes": [{"codigo": code, "nome": name, "perguntas": [
        {"id": q[0], "ordem": i, "texto": q[2], "opcoes": ["SIM", "NAO", "NAO_SEI"]}
        for i, q in enumerate(QUESTIONS, 1) if q[1] == code]} for code, name in DIMENSIONS]}


@app.post("/api/v1/locais/{local_id}/avaliacoes", status_code=201)
def create_review(local_id: int, payload: ReviewInput):
    validate_answers(payload.respostas)
    with DATA_LOCK:
        data = load_data(); get_local(data, local_id); stamp = now()
        next_id = max((item["id"] for item in data["avaliacoes"]), default=0) + 1
        data["next_avaliacao_id"] = next_id + 1
        review = {"id": next_id, "local_id": local_id,
                  "respostas": [x.model_dump() for x in payload.respostas], "criada_em": stamp, "atualizada_em": stamp}
        data["avaliacoes"].append(review); save_data(data)
        return review_output(review)


@app.get("/api/v1/locais/{local_id}/avaliacoes")
def list_reviews(local_id: int, pagina: int = Query(1, ge=1), limite: int = Query(20, ge=1, le=50)):
    with DATA_LOCK:
        data = load_data(); get_local(data, local_id)
        items = [r for r in data["avaliacoes"] if r["local_id"] == local_id]
        total = len(items); start = (pagina - 1) * limite
        return {"itens": [review_output(x) for x in items[start:start + limite]], "paginacao": pagination(total, pagina, limite)}


def get_review(data: dict, review_id: int) -> dict:
    review = next((x for x in data["avaliacoes"] if x["id"] == review_id), None)
    if not review:
        raise HTTPException(404, detail={"codigo": "AVALIACAO_NAO_ENCONTRADA", "mensagem": "Avaliação não encontrada."})
    return review


@app.get("/api/v1/avaliacoes/{review_id}")
def get_review_by_id(review_id: int):
    with DATA_LOCK: return review_output(get_review(load_data(), review_id))


@app.put("/api/v1/avaliacoes/{review_id}")
def update_review(review_id: int, payload: ReviewInput):
    validate_answers(payload.respostas)
    with DATA_LOCK:
        data = load_data(); review = get_review(data, review_id); review["respostas"] = [x.model_dump() for x in payload.respostas]; review["atualizada_em"] = now(); save_data(data)
        return review_output(review)


@app.delete("/api/v1/avaliacoes/{review_id}", status_code=204)
def delete_review(review_id: int):
    with DATA_LOCK:
        data = load_data(); review = get_review(data, review_id); data["avaliacoes"].remove(review); save_data(data); return Response(status_code=204)
