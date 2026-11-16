"""FastAPI application: revision-safe XBRL filing and reconciliation API."""
from __future__ import annotations

from typing import Optional

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, Header, Query, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from . import storage
from .db import init_db
from .errors import ApiError
from .models import (CheckAck, ExportOut, ExportRequest, InstanceOut, PackageOut,
                     ReportList, ReportOut)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="XBRL 财报勾稽核查 API",
    description=(
        "保存可修订的 XBRL 实例与计算关系包（XSD + calculation linkbase），"
        "按 decimals 舍入闭区间核查全部加权等式。返回同时满足全部等式的金额见证，"
        "或相互矛盾的事实/关系；缺引用事实则整份待补。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def _api_error(_request, exc: ApiError):
    body = {"error": {"code": exc.code, "message": exc.message}}
    if exc.details:
        body["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(RequestValidationError)
async def _validation_error(_request, exc: RequestValidationError):
    problems = [
        f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg', 'invalid')}"
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "validation_error", "message": "; ".join(problems)}},
    )


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


# ------------------------------------------------------------- packages
@app.post("/packages", response_model=PackageOut, status_code=201,
          tags=["packages"], summary="创建计算关系包（XSD + calculation linkbase）")
async def create_package(
    name: str = Form(..., description="关系包名称"),
    xsd: UploadFile = File(..., description="XSD taxonomy schema"),
    calculation: UploadFile = File(..., description="calculation linkbase XML"),
):
    return storage.create_package(name, await xsd.read(), await calculation.read())


@app.post("/packages/{package_id}/revisions", response_model=PackageOut,
          tags=["packages"], summary="修订关系包；Expected-Revision 过期则 412")
async def revise_package(
    package_id: str,
    xsd: UploadFile = File(...),
    calculation: UploadFile = File(...),
    expected_revision: int = Header(..., alias="Expected-Revision"),
):
    return storage.revise_package(
        package_id, expected_revision, await xsd.read(), await calculation.read()
    )


@app.get("/packages", response_model=list[PackageOut], tags=["packages"])
def list_packages():
    return storage.list_packages()


@app.get("/packages/{package_id}", response_model=PackageOut, tags=["packages"])
def get_package(package_id: str):
    return storage.get_package(package_id)


# ------------------------------------------------------------- instances
@app.post("/packages/{package_id}/instances", response_model=InstanceOut,
          status_code=201, tags=["instances"], summary="提交 XBRL 实例（首版）")
async def create_instance(package_id: str, instance: UploadFile = File(...)):
    return storage.create_instance(package_id, await instance.read())


@app.post("/instances/{instance_id}/revisions", response_model=InstanceOut,
          tags=["instances"],
          summary="更正实例；Expected-Package-Revision 过期则 412")
async def revise_instance(
    instance_id: str,
    instance: UploadFile = File(...),
    expected_package_revision: int = Header(..., alias="Expected-Package-Revision"),
):
    return storage.revise_instance(
        instance_id, expected_package_revision, await instance.read()
    )


@app.get("/instances", response_model=list[InstanceOut], tags=["instances"])
def list_instances(package_id: Optional[str] = Query(None)):
    return storage.list_instances(package_id)


@app.get("/instances/{instance_id}", response_model=InstanceOut, tags=["instances"])
def get_instance(instance_id: str):
    return storage.get_instance(instance_id)


# ------------------------------------------------------------- checks / reports
@app.post("/instances/{instance_id}/checks", response_model=CheckAck,
          tags=["checks"],
          summary="核查并生成报告；两侧修订与所见不一致则 412 拒绝")
def run_check(
    instance_id: str,
    expected_instance_revision: int = Header(..., alias="Expected-Instance-Revision"),
    expected_package_revision: int = Header(..., alias="Expected-Package-Revision"),
    late: bool = Query(False, description="显式声明为迟到补报：允许落后修订，仅存历史"),
):
    return storage.run_check(
        instance_id, expected_instance_revision, expected_package_revision, late
    )


@app.get("/instances/{instance_id}/reports", response_model=ReportList,
         tags=["reports"], summary="当前报告与全部历史（含迟到、已过期）")
def list_reports(instance_id: str):
    return storage.list_reports(instance_id)


@app.get("/reports/{report_id}", response_model=ReportOut, tags=["reports"])
def get_report(report_id: str):
    return storage.get_report(report_id)


@app.get("/reports/{report_id}/snapshots/{kind}", tags=["reports"],
         summary="下载报告保存的两份原始依据与实例（instance|xsd|calc）")
def report_snapshot(report_id: str, kind: str):
    if kind not in ("instance", "xsd", "calc"):
        from .errors import BadPayload
        raise BadPayload("kind must be one of instance, xsd, calc")
    data, media = storage.get_report_snapshot(report_id, kind)
    return Response(content=data, media_type=media)


# ------------------------------------------------- sanitized exports
@app.post("/exports", response_model=ExportOut, status_code=201, tags=["exports"],
          summary="脱敏供数：校验敏感科目可达宽度后发布仅含保留科目的 CSV")
def create_export(body: ExportRequest):
    sensitive = [{"concept": s.concept, "min_width": s.min_width}
                 for s in body.sensitive_concepts]
    return storage.create_export(
        body.report_id,
        body.expected_instance_revision,
        body.expected_package_revision,
        body.retained_concepts,
        sensitive,
    )


@app.get("/exports", response_model=list[ExportOut], tags=["exports"],
         summary="已发布的脱敏供数清单")
def list_exports(instance_id: Optional[str] = Query(None)):
    return storage.list_exports(instance_id)


@app.get("/exports/{export_id}", response_model=ExportOut, tags=["exports"])
def get_export(export_id: str):
    return storage.get_export(export_id)


@app.get("/exports/{export_id}/csv", tags=["exports"],
         summary="下载已发布的脱敏 CSV（仅保留科目的报告值与 decimals）")
def download_export(export_id: str):
    data, media = storage.get_export_csv(export_id)
    return Response(
        content=data,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{export_id}.csv"',
            "Cache-Control": "no-store",
        },
    )
