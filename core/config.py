"""Core configuration management."""

from typing import Optional, Dict, Any
from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache
import os


class Settings(BaseSettings):
    """Application settings."""
    
    # Core settings
    app_name: str = "Workflow Orchestrator"
    environment: str = Field(default="development", env="ENVIRONMENT")
    debug: bool = Field(default=False, env="DEBUG")
    
    # API settings
    api_host: str = Field(default="0.0.0.0", env="API_HOST")
    api_port: int = Field(default=8000, env="API_PORT")
    api_prefix: str = Field(default="/api/v1", env="API_PREFIX")
    
    # Database settings
    database_url: str = Field(
        default="sqlite+aiosqlite:///workflow.db",
        env="DATABASE_URL"
    )
    database_pool_size: int = Field(default=10, env="DATABASE_POOL_SIZE")
    
    # Redis settings
    redis_url: Optional[str] = Field(default=None, env="REDIS_URL")
    
    # Worker settings
    worker_concurrency: int = Field(default=10, env="WORKER_CONCURRENCY")
    worker_timeout: float = Field(default=300.0, env="WORKER_TIMEOUT")
    
    # Resource limits
    max_cpu_units: float = Field(default=4.0, env="MAX_CPU_UNITS")
    max_memory_mb: float = Field(default=4096.0, env="MAX_MEMORY_MB")
    max_io_weight: float = Field(default=100.0, env="MAX_IO_WEIGHT")
    max_network_weight: float = Field(default=100.0, env="MAX_NETWORK_WEIGHT")
    max_gpu_units: float = Field(default=0.0, env="MAX_GPU_UNITS")
    
    # Monitoring
    enable_metrics: bool = Field(default=True, env="ENABLE_METRICS")
    metrics_port: int = Field(default=9090, env="METRICS_PORT")
    otlp_endpoint: Optional[str] = Field(default=None, env="OTLP_ENDPOINT")
    
    # Feature flags
    enable_multitenancy: bool = Field(default=False, env="ENABLE_MULTITENANCY")
    enable_ai_optimizer: bool = Field(default=False, env="ENABLE_AI_OPTIMIZER")
    enable_marketplace: bool = Field(default=False, env="ENABLE_MARKETPLACE")
    enable_enterprise_auth: bool = Field(default=False, env="ENABLE_ENTERPRISE_AUTH")
    
    # Security
    secret_key: str = Field(default="changeme", env="SECRET_KEY")
    jwt_algorithm: str = Field(default="HS256", env="JWT_ALGORITHM")
    jwt_expiration_minutes: int = Field(default=30, env="JWT_EXPIRATION_MINUTES")
    
    # Storage
    storage_backend: str = Field(default="sqlite", env="STORAGE_BACKEND")
    checkpoint_interval: int = Field(default=60, env="CHECKPOINT_INTERVAL")
    
    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


# Feature flags helper
class Features:
    """Feature flags management."""
    
    def __init__(self, settings: Settings):
        self.settings = settings
    
    @property
    def multitenancy(self) -> bool:
        return self.settings.enable_multitenancy
    
    @property
    def ai_optimizer(self) -> bool:
        return self.settings.enable_ai_optimizer
    
    @property
    def marketplace(self) -> bool:
        return self.settings.enable_marketplace
    
    @property
    def enterprise_auth(self) -> bool:
        return self.settings.enable_enterprise_auth
    
    def is_enterprise(self) -> bool:
        """Check if any enterprise features are enabled."""
        return any([
            self.multitenancy,
            self.ai_optimizer,
            self.marketplace,
            self.enterprise_auth
        ])


@lru_cache()
def get_features() -> Features:
    """Get features instance."""
    return Features(get_settings())