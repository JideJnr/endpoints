from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from app.utils.mobile_bridge import (
    acknowledge_packets,
    list_provider_packets,
    mobile_bridge_status,
    receive_provider_packet,
)


router = APIRouter(prefix="/mobile-bridge", tags=["mobile-bridge"])


@router.post("/provider-packets")
def post_provider_packet(packet: dict[str, Any] = Body(...)):
    """
    Accept raw provider responses collected by the mobile app.

    SportyBet packets are immediately normalized into the normal match buffer.
    Other provider packets are stored first so parser hooks can be added without
    changing the mobile upload contract.
    """
    try:
        return receive_provider_packet(packet)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/provider-packets")
def get_provider_packets(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
):
    return list_provider_packets(status=status, limit=limit)


@router.get("/status")
def get_mobile_bridge_status():
    return mobile_bridge_status()


@router.post("/ack")
def post_mobile_bridge_ack(payload: dict[str, Any] = Body(...)):
    packet_ids = payload.get("packet_ids") or []
    if not isinstance(packet_ids, list):
        raise HTTPException(status_code=400, detail="packet_ids must be a list")
    return acknowledge_packets([str(item) for item in packet_ids])
