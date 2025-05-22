import requests
import json
import os
import base64
import datetime
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from dotenv import load_dotenv
import logging
import sqlite3
from sqlite3 import Error

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
code = os.getenv("CODE")

# Twilio credentials
twilio_account_sid = os.getenv("TWILIO_ACCOUNT_SID")
twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN")
twilio_whatsapp_number = os.getenv("TWILIO_WHATSAPP_NUMBER")

# URLs for M-Pesa
auth_url = "https://api.safaricom.co.ke/oauth/v2/generate?grant_type=client_credentials"
stk_push_url = "https://api.safaricom.co.ke/mpesa/stkpush/v1/processrequest"

# Database setup for tracking conversations
def create_connection():
    """Create a SQLite database connection"""
    conn = None
    try:
        conn = sqlite3.connect('conversations.db')
        return conn
    except Error as e:
        logger.error(f"Database error: {e}")
    return conn

def create_conversation_table():
    """Create table for tracking conversations between customers and feedback number"""
    conn = create_connection()
    if conn is not None:
        try:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    customer_number TEXT NOT NULL,
                    feedback_number TEXT NOT NULL,
                    last_message_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    payment_amount REAL,
                    active BOOLEAN DEFAULT TRUE
                )
            ''')
            conn.commit()
            logger.info("Conversation tracking table created or already exists")
        except Error as e:
            logger.error(f"Error creating table: {e}")
        finally:
            conn.close()
    else:
        logger.error("Error: cannot create database connection")

# Initialize database
create_conversation_table()

def track_conversation(customer_number, amount=None):
    """Create or update a conversation tracking record"""
    conn = create_connection()
    if conn is not None:
        try:
            cursor = conn.cursor()
            
            # Check if conversation already exists
            cursor.execute("SELECT id FROM conversations WHERE customer_number = ? AND feedback_number = ?", 
                          (customer_number, feedback_number))
            conversation = cursor.fetchone()
            
            if conversation:
                # Update existing conversation
                cursor.execute("""
                    UPDATE conversations 
                    SET last_message_time = CURRENT_TIMESTAMP,
                        payment_amount = COALESCE(?, payment_amount),
                        active = TRUE
                    WHERE customer_number = ? AND feedback_number = ?
                """, (amount, customer_number, feedback_number))
                logger.info(f"Updated conversation between {customer_number} and {feedback_number}")
            else:
                # Create new conversation
                cursor.execute("""
                    INSERT INTO conversations (customer_number, feedback_number, payment_amount)
                    VALUES (?, ?, ?)
                """, (customer_number, feedback_number, amount))
                logger.info(f"Created new conversation between {customer_number} and {feedback_number}")
            
            conn.commit()
        except Error as e:
            logger.error(f"Error tracking conversation: {e}")
        finally:
            conn.close()
    else:
        logger.error("Error: cannot create database connection")

def is_active_conversation(phone_number):
    """Check if there's an active conversation with this number"""
    conn = create_connection()
    if conn is not None:
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id FROM conversations 
                WHERE (customer_number = ? OR feedback_number = ?) 
                AND active = TRUE
            """, (phone_number, phone_number))
            result = cursor.fetchone()
            return result is not None
        except Error as e:
            logger.error(f"Error checking conversation: {e}")
        finally:
            conn.close()
    return False

def get_conversation_partner(phone_number):
    """Get the other person in the conversation"""
    conn = create_connection()
    if conn is not None:
        try:
            cursor = conn.cursor()
            # If this is a customer
            cursor.execute("""
                SELECT feedback_number FROM conversations 
                WHERE customer_number = ? AND active = TRUE
            """, (phone_number,))
            result = cursor.fetchone()
            
            if result:
                return result[0]
            
            # If this is the feedback number
            cursor.execute("""
                SELECT customer_number FROM conversations 
                WHERE feedback_number = ? AND active = TRUE
            """, (phone_number,))
            result = cursor.fetchone()
            
            if result:
                return result[0]
            
        except Error as e:
            logger.error(f"Error getting conversation partner: {e}")
        finally:
            conn.close()
    return None

def get_access_token():
    try:
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
        logger.error(f"Error initiating STK push: {str(e)}")
        return {"error": str(e)}

def send_whatsapp_message(to_number, message, from_number=None):
    """Send a WhatsApp message using Twilio client"""
    try:
        # Initialize the Twilio client
        client = Client(twilio_account_sid, twilio_auth_token)
        
        # Format the numbers for WhatsApp
        # Remove any "whatsapp:" prefix if it exists
        to_number = to_number.replace("whatsapp:", "")
        
        # Add WhatsApp prefix
        to_whatsapp = f"whatsapp:{to_number}"
        from_whatsapp = f"whatsapp:{twilio_whatsapp_number}" if from_number is None else f"whatsapp:{from_number}"
        
        logger.info(f"Sending WhatsApp message from {from_whatsapp} to {to_whatsapp}")
        
        # Send the message
        message = client.messages.create(
            body=message,
            from_=from_whatsapp,
            to=to_whatsapp
        )
        
        logger.info(f"Message sent successfully. SID: {message.sid}")
        return True
    except Exception as e:
        logger.error(f"Failed to send WhatsApp message: {str(e)}")
        return False

@app.post('/webhook')
async def webhook(request: Request):
    """Handle incoming WhatsApp messages"""
    logger.info("Webhook triggered")

    try:
        form = await request.form()
        incoming_msg = form.get('Body', '').strip()
        sender_number = form.get('From', '').replace('whatsapp:', '')

        logger.info(f"Incoming message: {incoming_msg}")
        logger.info(f"Sender number: {sender_number}")

        resp = MessagingResponse()

        # Check if this is a payment command
        if incoming_msg.lower().startswith('!dm pesa'):
            logger.info("Detected !dm pesa command")
            try:
                # Split the message into parts
                parts = incoming_msg.split()
                logger.info(f"Command parts: {parts}")

                # Validate command structure
                if len(parts) >= 3 and parts[0].lower() == '!dm' and parts[1].lower() == 'pesa':
                    try:
                        # Parse the amount
                        raw_amount = parts[2].strip('"').strip("'")
                        logger.info(f"Raw amount to be converted: '{raw_amount}'")
                        amount = float(raw_amount)
                        
                        # Call payment gateway (STK push)
                        stk_response = initiate_stk_push(sender_number, amount)
                        logger.info(f"STK Push response: {stk_response}")

                        # Check response from payment gateway
                        if 'ResponseCode' in stk_response and stk_response['ResponseCode'] == '0':
                            logger.info("STK push successful")
                            resp.message(f"Payment request of KES {amount} sent to your phone. Please enter your PIN to complete.")

                            # Create or update conversation tracking
                            track_conversation(sender_number, amount)

                            # Send notification to feedback number with client details
                            notification_message = f"New payment of KES {amount} initiated by customer. You can now chat directly with them."
                            send_whatsapp_message(feedback_number, notification_message)
                            
                            logger.info(f"Notification sent to {feedback_number}")
                        else:
                            error_msg = stk_response.get('errorMessage', 'Unknown error')
                            logger.warning(f"STK push failed: {error_msg}")
                            resp.message(f"Failed to initiate payment: {error_msg}")

                    except ValueError as ve:
                        logger.error(f"ValueError occurred: {ve}")
                        resp.message("Invalid amount. Please enter a numeric value like: !dm pesa 100")
                    except Exception as e:
                        logger.error(f"Error in payment processing: {str(e)}")
                        resp.message(f"An error occurred during payment processing: {str(e)}")

                else:
                    logger.warning("Invalid command structure")
                    resp.message("Invalid command format. Use: !dm pesa [amount]")

            except Exception as e:
                logger.error(f"Unexpected error processing the !dm pesa command: {str(e)}")
                resp.message(f"An error occurred: {str(e)}")

        # Check if this is part of an ongoing conversation
        elif is_active_conversation(sender_number):
            # Get the conversation partner
            recipient = get_conversation_partner(sender_number)
            
            if recipient:
                # Forward the message to the conversation partner
                forwarded = send_whatsapp_message(recipient, incoming_msg)
                
                if forwarded:
                    # No need to respond to the sender as we're just forwarding the message
                    resp.message("Message forwarded")
                else:
                    resp.message("Sorry, we couldn't forward your message. Please try again later.")
            else:
                resp.message("Could not find your conversation partner. Please try again later.")
        
        # If neither payment command nor part of conversation, show general help
        else:
            if sender_number == feedback_number:
                # This is the feedback number sending a message without active conversation
                resp.message("There are no active customer conversations. Wait for payment notifications.")
            else:
                # This is a regular user who isn't using the payment command
                resp.message("To make a payment, send: !dm pesa [amount]")

        return str(resp)

    except Exception as e:
        logger.critical(f"Critical error handling webhook: {str(e)}")
        return "Internal server error", 500

# Handle M-Pesa callback
@app.post('/mpesa-callback')
async def mpesa_callback(request: Request):
    """Handle callbacks from M-Pesa API"""
    try:
        data = await request.json()
        logger.info(f"M-Pesa callback received: {data}")
        
        # Extract transaction details if available
        transaction_amount = data.get('Amount', 'unknown amount')
        phone_number = data.get('PhoneNumber', 'unknown number')
        transaction_id = data.get('MpesaReceiptNumber', 'unknown')
        
        # Check if this is a successful transaction
        if data.get('ResultCode') == 0:
            # Find the conversation
            if phone_number != 'unknown number':
                # Update conversation tracking
                track_conversation(phone_number, transaction_amount)
                
                # Send confirmation to both parties
                customer_message = f"Your payment of KES {transaction_amount} (Receipt: {transaction_id}) was successful. You can now communicate directly with our support team."
                send_whatsapp_message(phone_number, customer_message)
                
                feedback_message = f"Payment of KES {transaction_amount} (Receipt: {transaction_id}) was completed by customer {phone_number}. You can now chat directly with them."
                send_whatsapp_message(feedback_number, feedback_message)
        
        return {
            "ResultCode": 0,
            "ResultDesc": "Callback received successfully"
        }
    except Exception as e:
        logger.error(f"Error processing M-Pesa callback: {str(e)}")
        return {
            "ResultCode": 1,
            "ResultDesc": f"Error processing callback: {str(e)}"
        }

@app.get('/')  
def home():
    return "server running"  

# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000)