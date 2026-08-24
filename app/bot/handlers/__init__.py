"""Роутеры основного бота. Порядок важен: fallback подключается последним."""

from __future__ import annotations

from aiogram import Router

from app.bot.handlers import earn, fallback, profile, reviews, shop, start, support, topup

ROUTERS: tuple[Router, ...] = (
    start.router,
    shop.router,
    reviews.router,
    topup.router,
    profile.router,
    earn.router,
    support.router,
    fallback.router,
)

__all__ = ["ROUTERS"]
