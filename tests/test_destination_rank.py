from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Destination
from app.routers.api import (
    DestinationReq,
    ReorderReq,
    create_destination,
    reorder_destinations,
)


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)()


def test_create_assigns_incrementing_rank_and_reorder_rewrites_it():
    s = _session()
    a = create_destination(DestinationReq(name="A", type="local", base_path="/a"), session=s)
    b = create_destination(DestinationReq(name="B", type="local", base_path="/b"), session=s)
    c = create_destination(DestinationReq(name="C", type="local", base_path="/c"), session=s)

    # New destinations are appended in creation order.
    assert a["rank"] < b["rank"] < c["rank"]

    reorder_destinations(ReorderReq(ordered_ids=[c["id"], a["id"], b["id"]]), session=s)

    ranks = {d.id: d.rank for d in s.query(Destination).all()}
    assert ranks[c["id"]] == 0
    assert ranks[a["id"]] == 1
    assert ranks[b["id"]] == 2
