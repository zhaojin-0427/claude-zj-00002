from typing import Union

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..response import ok
from ..schemas import ReadingBatchIn, ReadingIn
from ..services import ingest_service

router = APIRouter(prefix="/readings", tags=["数据接收"])


@router.post("", summary="电表数据上报（单条或批量，幂等）")
def submit_readings(body: Union[ReadingIn, ReadingBatchIn],
                    db: Session = Depends(get_db)):
    items = body.readings if isinstance(body, ReadingBatchIn) else [body]
    result = ingest_service.ingest_readings(db, items)
    return ok(result, message=f"接收 {result['accepted']} 条，重复 {result['duplicates']} 条")
