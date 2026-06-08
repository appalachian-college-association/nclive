# config.py
"""Configuration management for local environment"""

import os
import json
import logging
from typing import Dict, List

logger = logging.getLogger(__name__)

OCLC_DTYPES = {
    'marcOCN': 'str',
    'originalNCLiveOCN': 'str',
    'verifiedOCN': 'str',
    'oclcNumber': 'str',
    'lookupID': 'str',
    'lookupIDcollection': 'str',
    'source': 'str',
    'title': 'str',
    'collection_type': 'str'
}

class Config:
    """Configuration management for local environment"""

    def __init__(self):
        # Initialize configuration
        self._oclc_base_url = os.getenv(
            'OCLC_BASE_URL', 'https://discovery.api.oclc.org/worldcat-org-ci'
        )
        self._default_library = os.getenv('DEFAULT_LIBRARY', 'ACACL')
        self._restrict_to_library = os.getenv('RESTRICT_TO_LIBRARY', 'false').lower() == 'true'
        self._url_replace_chars = self._load_json_config(
            'URL_REPLACE_CHARS',
            default=['-', '–', '—', '―']
        )
        # Load secrets
        self._load_local_secrets()

    def _load_local_secrets(self):
        """Load secrets from local environment"""
        self.oclc_key = os.getenv('OCLC_KEY')
        self.oclc_secret = os.getenv('OCLC_SECRET')
        self.worldcat_kb_key = os.getenv('WORLDCAT_KB_KEY')

        missing = []
        if not self.oclc_key:
            missing.append('OCLC_KEY')
        if not self.oclc_secret:
            missing.append('OCLC_SECRET')
        if not self.worldcat_kb_key:
            missing.append('WORLDCAT_KB_KEY')

        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        logger.info("Local secrets loaded successfully")

    def _load_json_config(self, env_var: str, default: Dict = None) -> Dict:
        """
        Load and parse JSON configuration from environment variables
        
        Args:
            env_var: Name of environment variable
            default: Default value if env var is not set or invalid
        Returns:
            Parsed configuration or default value
        """
        try:
            value = os.getenv(env_var)
            if not value:
                return default if default is not None else {}
            return json.loads(value)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON in %s, using default", {env_var})
            return default if default is not None else {}

    # Properties
    @property
    def url_replace_chars(self) -> List[str]:
        """Characters to strip from URLs before processing."""
        return self._url_replace_chars

    @property
    def default_library(self) -> str:
        """Default OCLC library symbol for API queries."""
        return self._default_library

    @property
    def oclc_base_url(self) -> str:
        """Base URL for the OCLC Discovery API."""
        return self._oclc_base_url

    @property
    def restrict_to_library(self) -> bool:
        """True limits API search to default library holdings; False for global search"""
        return self._restrict_to_library
