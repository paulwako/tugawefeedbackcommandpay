import requests
import json
import os
import base64
import datetime
import uvicorn
from fastapi import FastAPI,Request
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
import logging


# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()


app = FastAPI()

# Safaricom M-Pesa API credentials
consumer_key = os.getenv("CONSUMER_KEY")
consumer_secret = os.getenv("CONSUMER_SECRET")
business_short_code = os.getenv("SHORT_CODE")  
passkey = os.getenv("PASSKEY")
callback_url = os.getenv("CALLBACK_URL")
till_number = os.getenv("TILL")
feedback_number = os.getenv("NUMBER")
code= os.getenv("CODE")

# URLs
auth_url = "https://api.safaricom.co.ke/oauth/v2/generate?grant_type=client_credentials"
stk_push_url = "https://api.safaricom.co.ke/mpesa/stkpush/v1/processrequest"

def get_access_token():
    try:
        # consumer_key = os.environ.get('MPESA_CONSUMER_KEY')
        # consumer_secret = os.environ.get('MPESA_CONSUMER_SECRET')
        
        if not consumer_key or not consumer_secret:
            print("Error: M-Pesa credentials not found in environment variables")
            raise HTTPException(status_code=500, detail="M-Pesa credentials not configured properly")
        
        url = "https://api.safaricom.co.ke/oauth/v2/generate?grant_type=client_credentials"
        payload = f"{consumer_key}:{consumer_secret}"
        encoded = base64.b64encode(payload.encode()).decode()

        response = requests.request("GET", url, headers = { 'Authorization': 'Basic {}'.format(encoded)})
        print(response.text.encode('utf8'))
        
        
        print(f"Response status code: {response.status_code}")
        print(f"Response body: {response.text[:100]}...") 
        
        # Check for errors
        response.raise_for_status()
        
        # Parse and return the access token
        data = response.json()
        if 'access_token' in data:
            print("Successfully retrieved access token")
            return data['access_token']
        else:
            print(f"No access token in response. Full response: {data}")
            raise HTTPException(status_code=500, detail="Invalid response format from Mpesa API")
        
    except requests.exceptions.RequestException as e:
        print(f"Authentication error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to authenticate with Mpesa API: {str(e)}")
    except KeyError as e:
        print(f"Key error in response: {str(e)}")
        raise HTTPException(status_code=500, detail="Unexpected response format from Mpesa API")
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {str(e)}")

def generate_password(shortcode, passkey, timestamp):
    """Generate password for STK push"""
    data_to_encode = shortcode + passkey + timestamp
    return base64.b64encode(data_to_encode.encode()).decode('utf-8')

def initiate_stk_push(phone_number, amount):
    """Initiate STK push to customer's phone"""
    
    logger.info(f"Initiating STK push for phone number: {phone_number} and amount: {amount}")
    
    try:
        # Get current timestamp
        timestamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
        logger.debug(f"Generated timestamp: {timestamp}")

        # Get access token
        access_token = get_access_token()
        logger.debug(f"Access token retrieved: {access_token}")

        # Prepare headers for the request
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        
        # Format phone number (remove leading 0 or +254 and add 254)
        if phone_number.startswith("+254"):
            phone_number = phone_number[1:]
            logger.debug(f"Phone number formatted: {phone_number}")
        elif phone_number.startswith("0"):
            phone_number = "254" + phone_number[1:]
            logger.debug(f"Phone number formatted: {phone_number}")
        
             
        
        # Prepare the payload
        payload = {
            "BusinessShortCode": business_short_code,
            "Password": generate_password(business_short_code, passkey, timestamp),
            "Timestamp": timestamp,
            "TransactionType": "CustomerBuyGoodsOnline",
            "Amount": amount,
            "PartyA": phone_number,
            "PartyB": till_number,
            "PhoneNumber": phone_number,
            "CallBackURL": callback_url,
            "AccountReference": "Payment to " + till_number,
            "TransactionDesc": "Payment via WhatsApp"
        }
        
        logger.debug(f"STK Push payload: {payload}")

        # Make the POST request
        response = requests.post(stk_push_url, json=payload, headers=headers)
        
        logger.debug(f"full response{response.json()}")
        # Log the response
        if response.status_code == 200:
            logger.info(f"STK push request successful: {response.json()}")
        else:
            logger.info(response.json())
            logger.info(payload)
            logger.error(f"STK push failed with status code {response.status_code}: {response.text}")
        
        # Return the response JSON
        return response.json()
    
    except Exception as e:
        # Log any errors that occur
        logger.error(f"here is the returned json {response.json()} ")
        logger.error(f"Error initiating STK push: {str(e)}")
        return {"error": str(e)}

@app.post('/webhook')
async def webhook(request: Request):
    """Handle incoming WhatsApp messages"""

    logger.info("Webhook triggered")

    try:
        form = await request.form()
        incoming_msg = form.get('Body', '').lower().strip()
        sender_number = form.get('From', '').replace('whatsapp:', '')

        logger.info(f"Incoming message: {incoming_msg}")
        logger.info(f"Sender number: {sender_number}")

        resp = MessagingResponse()

        # Process payment command
        if incoming_msg.startswith('!dm pesa'):
            logger.info("Detected !dm pesa command")
            try:
                # Split the message into parts
                parts = incoming_msg.split()
                logger.info(f"Command parts: {parts}")

                # Validate command structure
                if len(parts) >= 3 and parts[0] == '!dm' and parts[1] == 'pesa':
                    try:
                        # Log the part being processed before conversion
                        raw_amount = parts[2].strip('"').strip("'")
                        logger.info(f"Raw amount to be converted: '{raw_amount}'")

                        # Convert amount to float, remove surrounding quotes if any
                        amount = float(raw_amount)
                        logger.info(f"Converted amount: {amount}")
                        logger.info(f"Amount type: {type(amount)}")

                        # Call payment gateway (STK push)
                        stk_response = initiate_stk_push(sender_number, amount)
                        logger.info(f"STK Push response: {stk_response}")

                        # Check response from payment gateway
                        if 'ResponseCode' in stk_response and stk_response['ResponseCode'] == '0':
                            logger.info("STK push successful")
                            resp.message(f"Payment request of KES {amount} sent to your phone. Please enter your PIN to complete.")

                            # Send notification to feedback number
                            msg = resp.message("Hello", to=f"whatsapp:+{feedback_number}")
                            
                            logger.info(f"attempting to send a message to {feedback_number} ")
                            logger.info("Feedback message queued")
                        else:
                            error_msg = stk_response.get('errorMessage', 'Unknown error')
                            logger.warning(f"STK push failed: {error_msg}")
                            resp.message(f"Failed to initiate payment: {error_msg}")

                    except ValueError as ve:
                        # Log the specific error that occurred during the conversion
                        logger.error(f"ValueError occurred: {ve}")
                        logger.warning(f"Amount conversion failed for '{raw_amount}' - not a valid number")
                        resp.message("Invalid amount. Please enter a numeric value like: !dm pesa 100")
                    except Exception as e:
                        # Log any other exception
                        logger.error(f"Error in payment processing: {str(e)}")
                        resp.message(f"An error occurred during payment processing: {str(e)}")

                else:
                    logger.warning("Invalid command structure")
                    resp.message("Invalid command format. Use: !dm pesa [amount]")

            except Exception as e:
                logger.error(f"Unexpected error processing the !dm pesa command: {str(e)}")
                resp.message(f"An error occurred: {str(e)}")

        else:
            logger.info("Non-payment message received")
            resp.message("To make a payment, send: !dm pesa [amount]")

        return str(resp)

    except Exception as e:
        logger.critical(f"Critical error handling webhook: {str(e)}")
        return "Internal server error", 500

# Handle M-Pesa callback
@app.post('/mpesa-callback')
def mpesa_callback():
    """Handle callbacks from M-Pesa API"""
    # data = request.json
    
    return {
        "ResultCode": 0,
        "ResultDesc": "Callback received successfully"
    }

@app.get('/')  
def home():
    return "server running"  

# if __name__ == "__main__":
#     uvicorn app:app --host 0.0.0.0 --port 8000 --workers 4
