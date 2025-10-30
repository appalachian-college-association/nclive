# auth.py
from datetime import datetime, timedelta
from typing import Optional, Dict
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import base64
import structlog
from config import Config

logger = structlog.get_logger()

class OCLCAuth:
    """Handles OCLC authentication with improved token management"""
    
    def __init__(self):
        """Initialize authentication handler with retry strategy"""
        self.config = Config()
        self.key = self.config.OCLC_KEY
        self.secret = self.config.OCLC_SECRET
        
        if not self.key or not self.secret:
            raise ValueError("OCLC credentials not found")
            
        self.token_url = 'https://oauth.oclc.org/token'
        self.scope = ['WorldCatDiscoveryAPI:view_brief_bib']
        
        # Initialize token storage
        self._token = None
        self._token_expiry = None
        
        # Configure session with retry strategy
        self.session = self._create_session()
        
    def _create_session(self) -> requests.Session:
        """Create requests session with retry strategy"""
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session = requests.Session()
        session.mount("https://", adapter)
        
        return session
        
    def get_authorization_header(self) -> str:
        """Create Base64 encoded authorization header"""
        credentials = f"{self.key}:{self.secret}"
        encoded_credentials = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')
        return f"Basic {encoded_credentials}"
        
    def _is_token_valid(self) -> bool:
        """Check if current token is valid and not near expiration"""
        if not self._token or not self._token_expiry:
            return False
        return datetime.now() < self._token_expiry - timedelta(minutes=5)
        
    def get_token(self) -> Optional[Dict]:
        """Get an OAuth2 token using client credentials flow with caching"""
        try:
            # Return cached token if valid
            if self._is_token_valid():
                return self._token
                
            logger.info("requesting_new_token")
            response = self.session.post(
                self.token_url,
                headers={
                    'Authorization': self.get_authorization_header(),
                    'Content-Type': 'application/x-www-form-urlencoded'
                },
                data={
                    'grant_type': 'client_credentials',
                    'scope': ' '.join(self.scope)
                },
                timeout=10
            )
            
            response.raise_for_status()
            token_data = response.json()
            
            # Cache token and set expiry
            self._token = token_data
            self._token_expiry = datetime.now() + timedelta(seconds=token_data['expires_in'])
            
            logger.info("token_obtained")
            return token_data
            
        except requests.exceptions.RequestException as e:
            logger.error("token_request_failed", error=str(e))
            
            # Return cached token if available, even if expired
            if self._token:
                logger.warning("using_expired_token")
                return self._token
                
            logger.error("no_token_available")
            return None
            
    def get_valid_token(self) -> Optional[str]:
        """Get valid access token string or None"""
        token_data = self.get_token()
        if token_data and 'access_token' in token_data:
            return token_data['access_token']
        return None
