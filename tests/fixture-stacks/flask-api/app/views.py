from flask import Blueprint

bp = Blueprint("orders", __name__, url_prefix="/orders")


@bp.route("/<int:order_id>", methods=["GET", "DELETE"])
def order(order_id):
    return {"id": order_id}


@bp.get("/recent")
def recent():
    return []
