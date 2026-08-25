from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, alias_generators

from app.services.codex_auth import get_auth_status, logout_codex, start_device_auth
from app.services.strategist_provider import get_public_provider_settings, save_provider_settings

router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])


class StrategistProviderUpdateRequest(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    active_provider: str
    codex_model: str | None = None
    codex_reasoning_effort: str | None = None
    openai_api_key: str | None = None
    openai_model: str | None = None
    clear_openai_api_key: bool = False


@router.get("/chatgpt")
def chatgpt_auth_status() -> dict:
    try:
        return get_auth_status()
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load ChatGPT authentication status: {error}",
        ) from error


@router.post("/chatgpt/start")
def chatgpt_auth_start() -> dict:
    try:
        return start_device_auth()
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to start ChatGPT authentication: {error}",
        ) from error


@router.post("/chatgpt/logout")
def chatgpt_auth_logout() -> dict:
    try:
        return logout_codex()
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to disconnect ChatGPT authentication: {error}",
        ) from error


@router.get("/strategist-provider")
def strategist_provider_status(refresh_catalog: bool = False) -> dict:
    try:
        return get_public_provider_settings(force_catalog_refresh=refresh_catalog)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load strategist provider settings: {error}",
        ) from error


@router.post("/strategist-provider")
def strategist_provider_update(body: StrategistProviderUpdateRequest) -> dict:
    try:
        return save_provider_settings(
            active_provider=body.active_provider,
            codex_model=body.codex_model,
            codex_reasoning_effort=body.codex_reasoning_effort,
            openai_api_key=body.openai_api_key,
            openai_model=body.openai_model,
            clear_openai_api_key=body.clear_openai_api_key,
        )
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update strategist provider settings: {error}",
        ) from error
