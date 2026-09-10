from datetime import datetime
from sqlalchemy import String, Integer, Float, DateTime, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(240), index=True)
    brand: Mapped[str | None] = mapped_column(String(120), nullable=True)
    model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    gtin: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    image_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    offers: Mapped[list["Offer"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    source_links: Mapped[list["ProductSourceLink"]] = relationship(back_populates="product", cascade="all, delete-orphan")


class ProductSourceLink(Base):
    __tablename__ = "product_source_links"
    __table_args__ = (UniqueConstraint("provider_slug", "external_product_id", name="uq_source_external_product"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    provider_slug: Mapped[str] = mapped_column(String(60), index=True)
    external_product_id: Mapped[str] = mapped_column(String(160), index=True)
    source_url: Mapped[str | None] = mapped_column(String(1500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    product: Mapped[Product] = relationship(back_populates="source_links")


class Retailer(Base):
    __tablename__ = "retailers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    offers: Mapped[list["Offer"]] = relationship(back_populates="retailer")


class Offer(Base):
    __tablename__ = "offers"
    __table_args__ = (UniqueConstraint("retailer_id", "external_id", name="uq_offer_external"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    retailer_id: Mapped[int] = mapped_column(ForeignKey("retailers.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(240))
    title: Mapped[str] = mapped_column(String(500))
    seller_name: Mapped[str | None] = mapped_column(String(240), nullable=True)
    url: Mapped[str | None] = mapped_column(String(1500), nullable=True)
    active: Mapped[bool] = mapped_column(default=True)
    product: Mapped[Product] = relationship(back_populates="offers")
    retailer: Mapped[Retailer] = relationship(back_populates="offers")
    observations: Mapped[list["PriceObservation"]] = relationship(back_populates="offer", cascade="all, delete-orphan")


class PriceObservation(Base):
    __tablename__ = "price_observations"
    __table_args__ = (
        Index("ix_obs_offer_captured", "offer_id", "captured_at"),
        Index("ix_obs_product_captured", "product_id", "captured_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"), index=True)
    price: Mapped[float] = mapped_column(Float)
    pix_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    shipping_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="BRL")
    available: Mapped[bool] = mapped_column(default=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    source_kind: Mapped[str] = mapped_column(String(30), default="observed")
    source_name: Mapped[str] = mapped_column(String(120), default="our_collector")
    offer: Mapped[Offer] = relationship(back_populates="observations")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint("user_id", "product_id", name="uq_watch_user_product"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class IntegrationCredential(Base):
    """Tokens de integrações externas.

    Nesta fase do protótipo os tokens ficam no banco privado do backend. Antes
    de produção multiusuário, adicionaremos criptografia em repouso e vínculo
    por usuário/conta.
    """
    __tablename__ = "integration_credentials"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_slug: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    access_token: Mapped[str] = mapped_column(String(4000))
    refresh_token: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
