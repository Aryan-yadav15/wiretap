from app.database import Db


def compute_total(items, method=None):
    subtotal = sum(i["price"] for i in items)
    return subtotal + pay_later_fee(method)


def pay_later_fee(method):
    if method != "cod":
        return 0
    row = Db.table("charges").select("amount").eq("kind", "pay_later").execute()
    return row.data[0]["amount"]
