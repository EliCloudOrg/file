"""请求体模型（只有 PATCH 可见性与授权两个小请求体需要）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class VisibilityPatch(BaseModel):
    """``PATCH /v1/files/{id}``：目前只允许改可见性。"""

    model_config = ConfigDict(extra="forbid")

    visibility: str = Field(description="public / private / protected")


class GrantRequest(BaseModel):
    """``POST /v1/files/{id}/grants``：被授权者的 SSO sub。"""

    model_config = ConfigDict(extra="forbid")

    grantee_sub: str = Field(description="被授权用户的 SSO sub（稳定，永不变更）")
