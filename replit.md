# Litmos User Management Tool

## Project Overview
A Flask-based web application for bulk activation and deactivation of Litmos users via CSV upload. The application processes CSV files containing 30-100 users per upload and communicates with the Litmos API to manage user statuses.

## Recent Changes (September 30, 2025)

### CSV Validation (30-100 Users)
- **Frontend Validation**: Updated `static/js/main.js` to validate CSV files contain between 30-100 users before upload
- **Backend Validation**: Enhanced `app.py` to enforce MIN_RECORDS=30 and MAX_RECORDS=100 per CSV
- **UI Updates**: Modified all templates (index.html, activation.html, deactivation.html) to display CSV requirements prominently

### Fixed JSON Parsing Error
- **Root Cause**: API was returning HTML error pages instead of JSON when overloaded or timing out
- **Solution**: Enhanced `parse_response()` function in app.py to:
  - Detect HTML responses early and log them as errors
  - Handle JSON parsing exceptions gracefully
  - Add detailed debug logging for troubleshooting
  - Return descriptive error messages instead of failing silently

### Batch Processing & Rate Limiting
- **Connection Pooling**: HTTP session with retry logic and connection pool (10-20 connections)
- **Concurrency Control**: Semaphore limiting concurrent API calls (max 4 in-flight requests)
- **Delay Between Calls**: 0.02s delay between user operations to prevent API overload
- **Worker Pool**: ThreadPoolExecutor with 2 workers for background CSV processing

### Cloud Run Optimization
- **Port Configuration**: App reads PORT from environment (defaults to 5000 for local, 8080 for Cloud Run)
- **Gunicorn Setup**: Dockerfile configured with gthread workers for proper threading support
- **Timeout Settings**: Configurable timeouts for HTTP requests and worker processes
- **Health Endpoint**: `/health` endpoint for Cloud Run health checks

## Architecture

### Key Components
- **Flask Backend** (`app.py`): Main application with routes, CSV processing, and Litmos API integration
- **Frontend** (`templates/`, `static/`): Bootstrap-based UI with JavaScript validation
- **CSV Processing**: Background worker threads with job status tracking
- **Error Handling**: Comprehensive error handling with debug logging

### API Routes
- `GET /` - Home page with instructions
- `GET /activation` - Bulk activation page
- `GET /deactivation` - Bulk deactivation page
- `GET /results` - Results display page
- `POST /api/process-csv` - CSV upload and processing endpoint
- `GET /api/job-status/<job_id>` - Job status polling endpoint
- `GET /health` - Health check endpoint

### Environment Variables
Required:
- `SESSION_SECRET` - Flask session secret key
- `LITMOS_API_KEY` - Litmos API authentication key
- `LITMOS_BASE_URL` - Litmos API base URL (default: https://api.litmos.com/v1.svc)

CSV Limits:
- `MIN_RECORDS` - Minimum users per CSV (default: 30)
- `MAX_RECORDS` - Maximum users per CSV (default: 100)

Optional (for Cloud Run):
- `PORT` - Server port (default: 5000 local, 8080 Cloud Run)
- `GOOGLE_CLIENT_ID` - For Google OAuth authentication
- `ALLOWED_DOMAIN` - Allowed email domain for authentication

See `.env.example` for complete configuration.

## Deployment

### Local Development
```bash
python app.py
```
Server runs on http://localhost:5000

### Google Cloud Run
```bash
gcloud builds submit --tag gcr.io/[PROJECT-ID]/litmos-tool
gcloud run deploy litmos-tool --image gcr.io/[PROJECT-ID]/litmos-tool --platform managed
```

The Dockerfile is optimized for Cloud Run with:
- Gunicorn with gthread workers
- Configurable worker count and timeout
- Environment variable port binding

## User Preferences
- None specified yet

## Known Issues
- LSP import warnings (harmless - packages are installed correctly)
- Favicon 404 errors (normal - no favicon configured)

## Dependencies
- Flask 3.1.1 - Web framework
- flask-cors 6.0.1 - CORS support
- requests 2.32.4 - HTTP client with retry logic
- gunicorn 23.0.0 - Production WSGI server
- google-auth 2.40.3 - Google OAuth (optional)
- python-dotenv 1.1.1 - Environment variable management
