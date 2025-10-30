# config.py
import os
import json
import logging
from typing import Dict, List

logger = logging.getLogger(__name__)

class Config:
    """Configuration management for local environment"""
    
    def __init__(self):
        # Initialize configuration
        self._oclc_base_url = os.getenv('OCLC_BASE_URL', 'https://discovery.api.oclc.org/worldcat-org-ci')
        self._default_library = os.getenv('DEFAULT_LIBRARY', 'ACACL')
        self._restrict_to_library = os.getenv('RESTRICT_TO_LIBRARY', 'false').lower() == 'true'
        self._url_replace_chars = self._load_json_config(
            'URL_REPLACE_CHARS',
            default=['-', '–', '—', '―']
        )
        self.OCLC_DTYPES = {
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
        self.MAX_RESULTS_PER_PAGE = int(os.getenv('MAX_RESULTS_PER_PAGE', '50'))
        self.DEFAULT_RESULTS_PER_PAGE = int(os.getenv('DEFAULT_RESULTS_PER_PAGE', '10'))
        
        # Load secrets
        self._load_local_secrets()
        
    def _load_local_secrets(self):
        """Load secrets from local environment"""
        self.OCLC_KEY = os.getenv('OCLC_KEY')
        self.OCLC_SECRET = os.getenv('OCLC_SECRET')
        self.WORLDCAT_KB_KEY = os.getenv('WORLDCAT_KB_KEY')

        missing = []
        if not self.OCLC_KEY:
            missing.append('OCLC_KEY')
        if not self.OCLC_SECRET:
            missing.append('OCLC_SECRET')
        if not self.WORLDCAT_KB_KEY:
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
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON in {env_var}, using default")
            return default if default is not None else {}

    # Properties
    @property
    def URL_REPLACE_CHARS(self) -> List[str]:
        return self._url_replace_chars
      
    @property
    def DEFAULT_LIBRARY(self) -> str:
        return self._default_library
        
    @property 
    def OCLC_BASE_URL(self) -> str:
        return self._oclc_base_url
    
    @property
    def RESTRICT_TO_LIBRARY(self) -> bool:
        return self._restrict_to_library