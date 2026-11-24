"""Cart API tests, against a real PostgreSQL (same TEST_* settings and fixtures as test_regressions.py).

Contract under test:
  GET    /api/v1/cart                       -> 200 {"items": [...], "total": ...}
  POST   /api/v1/cart/items {product_id, quantity}  -> 200 + cart (quantity is added to the line)
  PUT    /api/v1/cart/items/{product_id} {quantity} -> 200 + cart (quantity replaces the line's)
  DELETE /api/v1/cart/items/{product_id}    -> 200 + cart
Each item: {"product_id", "quantity", "unit_price", "line_total"}. Prices always come from the product.
"""

import asyncio
import itertools
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

# The database fixtures live in test_regressions.py; importing them here avoids copying them.
from tests.test_regressions import bearer, client, engine, maker, register  # noqa: F401

CART = "/api/v1/cart"
product_numbers = itertools.count(1)


# -------------------------------------------------------------------------- helpers


async def make_product(maker, *, price: str = "10.00", stock: int = 100) -> int:
    from app.models.product import Product

    n = next(product_numbers)
    async with maker() as session:
        product = Product(
            name=f"Product {n}", slug=f"product-{n}", sku=f"SKU-{n}", price=Decimal(price), inventory_count=stock
        )
        session.add(product)
        await session.commit()
        return product.id


async def set_price(maker, product_id: int, price: str) -> None:
    from app.models.product import Product

    async with maker() as session:
        await session.execute(update(Product).where(Product.id == product_id).values(price=Decimal(price)))
        await session.commit()


async def new_user(client, email: str) -> dict:
    response = await register(client, email)
    assert response.status_code == 201, response.text
    return bearer(response.json())


async def add(client, headers, product_id: int, quantity: int = 1, **extra):
    return await client.post(
        f"{CART}/items", headers=headers, json={"product_id": product_id, "quantity": quantity, **extra}
    )


async def put(client, headers, product_id: int, quantity: int):
    return await client.put(f"{CART}/items/{product_id}", headers=headers, json={"quantity": quantity})


async def remove(client, headers, product_id: int):
    return await client.delete(f"{CART}/items/{product_id}", headers=headers)


async def get_cart(client, headers) -> dict:
    response = await client.get(CART, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def quantities(cart: dict) -> dict[int, int]:
    return {item["product_id"]: item["quantity"] for item in cart["items"]}


def money(value) -> Decimal:
    return Decimal(str(value))


async def stored_cart(maker, email: str) -> tuple[int, dict[int, int]]:
    """Straight from the database: how many cart rows the user has, and {product_id: quantity}."""
    from app.models.cart import Cart, CartItem
    from app.models.user import User

    async with maker() as session:
        user_id = (await session.execute(select(User.id).where(User.email == email))).scalar_one()
        carts = (
            await session.execute(select(func.count()).select_from(Cart).where(Cart.user_id == user_id))
        ).scalar_one()
        rows = (
            await session.execute(
                select(CartItem.product_id, CartItem.quantity)
                .join(Cart, Cart.id == CartItem.cart_id)
                .where(Cart.user_id == user_id)
            )
        ).all()
    return carts, {product_id: quantity for product_id, quantity in rows}


# ------------------------------------------------------------------------ basic flow


async def test_empty_cart_is_returned_for_a_new_user(client) -> None:
    headers = await new_user(client, "empty@example.com")

    cart = await get_cart(client, headers)

    assert cart["items"] == []
    assert money(cart["total"]) == 0


async def test_add_item_to_cart(client, maker) -> None:
    headers = await new_user(client, "add@example.com")
    product_id = await make_product(maker, price="10.00")

    response = await add(client, headers, product_id, 2)

    assert response.status_code == 200, response.text
    assert quantities(response.json()) == {product_id: 2}
    assert quantities(await get_cart(client, headers)) == {product_id: 2}


async def test_adding_the_same_product_again_sums_the_quantities(client, maker) -> None:
    headers = await new_user(client, "again@example.com")
    product_id = await make_product(maker)

    await add(client, headers, product_id, 2)
    response = await add(client, headers, product_id, 3)

    assert response.status_code == 200, response.text
    assert quantities(await get_cart(client, headers)) == {product_id: 5}


async def test_put_sets_the_quantity(client, maker) -> None:
    headers = await new_user(client, "put@example.com")
    product_id = await make_product(maker, price="4.00")
    await add(client, headers, product_id, 2)

    response = await put(client, headers, product_id, 7)

    assert response.status_code == 200, response.text
    cart = await get_cart(client, headers)
    assert quantities(cart) == {product_id: 7}
    assert money(cart["items"][0]["line_total"]) == Decimal("28.00")


async def test_delete_removes_the_line(client, maker) -> None:
    headers = await new_user(client, "delete@example.com")
    first = await make_product(maker, price="10.00")
    second = await make_product(maker, price="2.50")
    await add(client, headers, first, 1)
    await add(client, headers, second, 2)

    response = await remove(client, headers, first)

    assert response.status_code == 200, response.text
    cart = await get_cart(client, headers)
    assert quantities(cart) == {second: 2}
    assert money(cart["total"]) == Decimal("5.00")


# -------------------------------------------------------------------------- prices


async def test_cart_computes_line_totals_and_total_on_the_server(client, maker) -> None:
    headers = await new_user(client, "totals@example.com")
    first = await make_product(maker, price="19.99")
    second = await make_product(maker, price="5.50")
    await add(client, headers, first, 3)
    await add(client, headers, second, 2)

    cart = await get_cart(client, headers)

    by_product = {item["product_id"]: item for item in cart["items"]}
    assert money(by_product[first]["unit_price"]) == Decimal("19.99")
    assert money(by_product[first]["line_total"]) == Decimal("59.97")
    assert money(by_product[second]["line_total"]) == Decimal("11.00")
    assert money(cart["total"]) == Decimal("70.97")


async def test_cart_shows_the_current_price_after_the_product_price_changes(client, maker) -> None:
    headers = await new_user(client, "pricechange@example.com")
    product_id = await make_product(maker, price="10.00")
    await add(client, headers, product_id, 2)

    await set_price(maker, product_id, "12.50")
    cart = await get_cart(client, headers)

    assert money(cart["items"][0]["unit_price"]) == Decimal("12.50")
    assert money(cart["items"][0]["line_total"]) == Decimal("25.00")
    assert money(cart["total"]) == Decimal("25.00")


async def test_client_cannot_set_the_price(client, maker) -> None:
    headers = await new_user(client, "cheat@example.com")
    product_id = await make_product(maker, price="10.00")

    response = await add(client, headers, product_id, 1, unit_price="0.01")

    assert response.status_code == 200, response.text
    cart = await get_cart(client, headers)
    assert money(cart["items"][0]["unit_price"]) == Decimal("10.00")
    assert money(cart["total"]) == Decimal("10.00")


# ------------------------------------------------------------------ auth and ownership


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", CART, None),
        ("POST", f"{CART}/items", {"product_id": 1, "quantity": 1}),
        ("PUT", f"{CART}/items/1", {"quantity": 1}),
        ("DELETE", f"{CART}/items/1", None),
    ],
)
async def test_cart_needs_a_token(client, method: str, path: str, body) -> None:
    response = await client.request(method, path, json=body)

    assert response.status_code == 401


async def test_another_user_never_sees_my_cart(client, maker) -> None:
    mine = await new_user(client, "mine@example.com")
    theirs = await new_user(client, "theirs@example.com")
    my_product = await make_product(maker)
    their_product = await make_product(maker)
    assert (await add(client, mine, my_product, 4)).status_code == 200

    assert (await get_cart(client, theirs))["items"] == []

    assert (await add(client, theirs, their_product, 1)).status_code == 200
    await remove(client, theirs, my_product)  # must not touch my line, whatever it answers
    await put(client, theirs, my_product, 9)  # same

    assert quantities(await get_cart(client, mine)) == {my_product: 4}
    assert quantities(await get_cart(client, theirs)) == {their_product: 1}


# ------------------------------------------------------------------------ validation


async def test_unknown_product_is_404(client) -> None:
    headers = await new_user(client, "unknown@example.com")

    for response in (await add(client, headers, 999999, 1), await put(client, headers, 999999, 1)):
        assert response.status_code == 404
        # A missing route also answers 404 ("Not Found"), so check that this is about the product.
        assert "product" in response.json()["detail"].lower()


@pytest.mark.parametrize("quantity", [0, -1, 100])
async def test_quantity_outside_1_to_99_is_422(client, maker, quantity: int) -> None:
    headers = await new_user(client, "badqty@example.com")
    product_id = await make_product(maker)

    assert (await add(client, headers, product_id, quantity)).status_code == 422
    assert (await put(client, headers, product_id, quantity)).status_code == 422

    assert (await get_cart(client, headers))["items"] == []


async def test_a_line_can_reach_99_but_not_more(client, maker) -> None:
    headers = await new_user(client, "cap@example.com")
    product_id = await make_product(maker, stock=1000)

    assert (await add(client, headers, product_id, 60)).status_code == 200
    assert (await add(client, headers, product_id, 40)).status_code == 422
    assert quantities(await get_cart(client, headers)) == {product_id: 60}

    assert (await add(client, headers, product_id, 39)).status_code == 200
    assert (await add(client, headers, product_id, 1)).status_code == 422
    assert quantities(await get_cart(client, headers)) == {product_id: 99}


async def test_more_than_the_stock_is_409(client, maker) -> None:
    headers = await new_user(client, "stock@example.com")
    product_id = await make_product(maker, stock=5)

    assert (await add(client, headers, product_id, 6)).status_code == 409
    assert (await add(client, headers, product_id, 3)).status_code == 200
    assert (await add(client, headers, product_id, 3)).status_code == 409  # 3 + 3 > 5
    assert (await put(client, headers, product_id, 6)).status_code == 409
    assert quantities(await get_cart(client, headers)) == {product_id: 3}

    assert (await add(client, headers, product_id, 2)).status_code == 200  # exactly the stock
    assert quantities(await get_cart(client, headers)) == {product_id: 5}


@pytest.mark.parametrize("quantity", ["abc", 1.5])
async def test_a_quantity_that_is_not_a_whole_number_is_422(client, maker, quantity) -> None:
    headers = await new_user(client, "fraction@example.com")
    product_id = await make_product(maker)
    await add(client, headers, product_id, 2)

    assert (await add(client, headers, product_id, quantity)).status_code == 422
    assert (await put(client, headers, product_id, quantity)).status_code == 422

    assert quantities(await get_cart(client, headers)) == {product_id: 2}


async def test_put_for_a_product_that_is_not_in_the_cart_is_404(client, maker) -> None:
    headers = await new_user(client, "put-missing@example.com")
    in_cart = await make_product(maker)
    not_in_cart = await make_product(maker)
    await add(client, headers, in_cart, 1)

    response = await put(client, headers, not_in_cart, 3)

    assert response.status_code == 404
    # A missing route also answers 404 ("Not Found"), so check that this is about the cart line.
    assert "cart" in response.json()["detail"].lower()
    assert quantities(await get_cart(client, headers)) == {in_cart: 1}


@pytest.mark.parametrize("which", ["not_in_cart", "unknown"])
async def test_delete_of_a_missing_line_is_200_and_returns_the_cart(client, maker, which: str) -> None:
    headers = await new_user(client, "delete-missing@example.com")
    in_cart = await make_product(maker)
    not_in_cart = await make_product(maker)
    await add(client, headers, in_cart, 1)
    target = not_in_cart if which == "not_in_cart" else 999999

    response = await remove(client, headers, target)

    assert response.status_code == 200, response.text
    assert quantities(response.json()) == {in_cart: 1}
    assert quantities(await get_cart(client, headers)) == {in_cart: 1}


# ------------------------------------------------------------------------ concurrency


async def test_20_parallel_adds_of_the_same_product_sum_up(client, maker) -> None:
    email = "parallel-add@example.com"
    headers = await new_user(client, email)
    product_id = await make_product(maker, stock=1000)

    responses = await asyncio.gather(*(add(client, headers, product_id, 1) for _ in range(20)))

    assert [r.status_code for r in responses] == [200] * 20
    assert await stored_cart(maker, email) == (1, {product_id: 20})


async def test_parallel_adds_cannot_push_a_line_over_99(client, maker) -> None:
    """20 requests of 10: exactly 9 fit (90). A read-then-check would let more through."""
    email = "parallel-cap@example.com"
    headers = await new_user(client, email)
    product_id = await make_product(maker, stock=1000)

    responses = await asyncio.gather(*(add(client, headers, product_id, 10) for _ in range(20)))

    assert sorted(r.status_code for r in responses) == [200] * 9 + [422] * 11
    assert await stored_cart(maker, email) == (1, {product_id: 90})


async def test_20_parallel_first_cart_creations_make_one_cart(client, maker) -> None:
    """A brand-new user (no cart row yet) fires 20 adds at once, each for a different product."""
    email = "parallel-first@example.com"
    headers = await new_user(client, email)
    product_ids = [await make_product(maker) for _ in range(20)]
    # Open the pool's connections first. With a cold pool the first request finishes before the
    # others have connected, and the race this test is about never happens.
    other = await new_user(client, "parallel-first-warmup@example.com")
    await asyncio.gather(*(client.get(CART, headers=other) for _ in range(20)))

    responses = await asyncio.gather(*(add(client, headers, pid, 1) for pid in product_ids))

    assert [r.status_code for r in responses] == [200] * 20
    assert await stored_cart(maker, email) == (1, {pid: 1 for pid in product_ids})


async def test_20_parallel_reads_by_a_new_user_do_not_fail(client, maker) -> None:
    email = "parallel-read@example.com"
    headers = await new_user(client, email)

    responses = await asyncio.gather(*(client.get(CART, headers=headers) for _ in range(20)))

    assert [r.status_code for r in responses] == [200] * 20
    carts, items = await stored_cart(maker, email)
    assert carts <= 1
    assert items == {}
